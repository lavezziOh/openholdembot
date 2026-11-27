"""Hand history writer — saves each hand as JSON + optional PokerStars text format.

Called by the engine after every hand. Produces:
  - hands/{table_id}.jsonl  — one JSON object per line, machine-readable
  - hands/{table_id}.txt    — PokerStars-style HH, human-readable (optional)
"""
import json
import os
import time
from datetime import datetime, timezone

RANKS = "23456789TJQKA"
SUITS = "cdhs"
SUIT_SYMBOLS = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}
STREET_NAMES = {1: "preflop", 2: "flop", 3: "turn", 4: "river"}


def card_str(c):
    """(rank, suit) tuple -> 'Ah', 'Td', etc."""
    return RANKS[c[0] - 2] + SUITS[c[1]]


def _position_label(chair, dealer, n_seats):
    """Return position name for a chair relative to the dealer."""
    dist = (chair - dealer) % n_seats
    if n_seats == 2:
        return "BTN" if dist == 0 else "BB"
    labels = {0: "BTN"}
    labels[1] = "SB"
    labels[2] = "BB"
    remaining = n_seats - 3
    # UTG, UTG+1, ..., HJ, CO
    pos_names = []
    if remaining >= 1:
        pos_names.append("CO")
    if remaining >= 2:
        pos_names.insert(0, "HJ")
    if remaining >= 3:
        pos_names.insert(0, "LJ")
    for i in range(remaining - 3):
        pos_names.insert(0, f"UTG+{i + 1}" if i > 0 else "UTG")
    if remaining > 3:
        pos_names.insert(0, "UTG")
    for i, name in enumerate(pos_names):
        labels[3 + i] = name
    return labels.get(dist, f"Seat{dist}")


class HandHistoryWriter:
    def __init__(self, out_dir, table_id=0, sb=0.5, bb=1.0, write_text=True):
        self.out_dir = out_dir
        self.table_id = table_id
        self.sb_val = sb
        self.bb_val = bb
        self.write_text = write_text
        os.makedirs(out_dir, exist_ok=True)
        self.jsonl_path = os.path.join(out_dir, f"table_{table_id}.jsonl")
        self.txt_path = os.path.join(out_dir, f"table_{table_id}.txt")

    def save(self, engine, result):
        """Call after play_hand() returns. Reads engine state + result dict."""
        hand = self._build_hand(engine, result)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(hand, ensure_ascii=False) + "\n")
        if self.write_text:
            with open(self.txt_path, "a", encoding="utf-8") as f:
                f.write(self._to_pokerstars(hand) + "\n\n")
        return hand

    def _build_hand(self, eng, result):
        ts = datetime.now(timezone.utc).isoformat()
        seats = []
        for s in eng.seats:
            seat = {
                "chair": s.chair,
                "name": s.name,
                "stack_start": round(s.start_stack, 2),
                "stack_end": round(s.stack, 2),
                "position": _position_label(s.chair, eng.dealer, eng.n),
                "hole": [card_str(c) for c in s.hole] if s.hole else [],
                "folded": s.folded,
                "invested": round(s.invested, 2),
            }
            seats.append(seat)

        actions_by_street = {1: [], 2: [], 3: [], 4: []}
        for rec in eng.history:
            actions_by_street.setdefault(rec.street, []).append({
                "player": eng.seats[rec.chair].name,
                "chair": rec.chair,
                "action": rec.kind,
                "to": round(rec.to, 2),
                "added": round(rec.added, 2),
                "allin": rec.allin,
            })

        board_cards = [card_str(c) for c in result.get("board", eng.board)]

        hand = {
            "hand_id": eng.hand_no,
            "table_id": self.table_id,
            "timestamp": ts,
            "sb": self.sb_val,
            "bb": self.bb_val,
            "dealer": eng.dealer,
            "n_seats": eng.n,
            "seats": seats,
            "board": board_cards,
            "preflop": actions_by_street.get(1, []),
            "flop": actions_by_street.get(2, []),
            "turn": actions_by_street.get(3, []),
            "river": actions_by_street.get(4, []),
            "showdown": result.get("showdown", False),
            "pot": round(sum(s.invested for s in eng.seats), 2),
            "rake": round(getattr(eng, "last_rake", 0.0), 2),
            "payouts": {eng.seats[c].name: round(v, 2) for c, v in result["payouts"].items()},
        }
        return hand

    def _to_pokerstars(self, h):
        """Generate PokerStars-compatible text hand history."""
        lines = []
        hid = h["hand_id"]
        tid = h["table_id"]
        ts = h["timestamp"][:19].replace("T", " ")
        sb, bb = h["sb"], h["bb"]
        lines.append(f"PokerStars Hand #{hid}: Hold'em No Limit (${sb:.2f}/${bb:.2f} USD) - {ts} UTC")
        lines.append(f"Table 'Sim Table {tid + 1}' {h['n_seats']}-max Seat #{h['dealer'] + 1} is the button")

        for s in h["seats"]:
            lines.append(f"Seat {s['chair'] + 1}: {s['name']} (${s['stack_start']:.2f} in chips)")

        # blinds & antes from preflop actions
        pre = h["preflop"]
        blind_end = 0
        for i, a in enumerate(pre):
            if a["action"] in ("sb", "bb", "ante"):
                lines.append(f"{a['player']}: posts {a['action']} ${a['added']:.2f}")
                blind_end = i + 1
            else:
                break

        lines.append("*** HOLE CARDS ***")
        for s in h["seats"]:
            if s["hole"]:
                lines.append(f"Dealt to {s['name']} [{' '.join(s['hole'])}]")

        # preflop actions (after blinds)
        for a in pre[blind_end:]:
            lines.append(self._action_line(a))

        board = h["board"]
        if h["flop"]:
            lines.append(f"*** FLOP *** [{' '.join(board[:3])}]")
            for a in h["flop"]:
                lines.append(self._action_line(a))

        if h["turn"]:
            lines.append(f"*** TURN *** [{' '.join(board[:3])}] [{board[3]}]")
            for a in h["turn"]:
                lines.append(self._action_line(a))

        if h["river"]:
            lines.append(f"*** RIVER *** [{' '.join(board[:4])}] [{board[4]}]")
            for a in h["river"]:
                lines.append(self._action_line(a))

        if h["showdown"]:
            lines.append("*** SHOW DOWN ***")
            for s in h["seats"]:
                if s["hole"] and not s["folded"]:
                    lines.append(f"{s['name']}: shows [{' '.join(s['hole'])}]")

        lines.append("*** SUMMARY ***")
        lines.append(f"Total pot ${h['pot']:.2f} | Rake ${h['rake']:.2f}")
        if board:
            lines.append(f"Board [{' '.join(board[:5])}]")

        for s in h["seats"]:
            payout = h["payouts"].get(s["name"], 0)
            net = round(payout - s["invested"], 2)
            if payout > 0:
                lines.append(f"Seat {s['chair'] + 1}: {s['name']} ({s['position']}) collected ${payout:.2f}")
            elif s["folded"]:
                lines.append(f"Seat {s['chair'] + 1}: {s['name']} ({s['position']}) folded")
            else:
                lines.append(f"Seat {s['chair'] + 1}: {s['name']} ({s['position']}) lost")

        return "\n".join(lines)

    @staticmethod
    def _action_line(a):
        name = a["player"]
        kind = a["action"]
        if kind == "fold":
            return f"{name}: folds"
        if kind == "check":
            return f"{name}: checks"
        if kind == "call":
            s = f"{name}: calls ${a['added']:.2f}"
            if a["allin"]:
                s += " and is all-in"
            return s
        if kind in ("bet", "raise"):
            s = f"{name}: {'bets' if kind == 'bet' else 'raises to'} ${a['to']:.2f}"
            if a["allin"]:
                s += " and is all-in"
            return s
        return f"{name}: {kind} ${a['added']:.2f}"
