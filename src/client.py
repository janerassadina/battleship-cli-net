#!/usr/bin/env python3
"""Battleship client.

Usage: python3 client.py -i server.smith.edu -p 8080
Type "quit" (or press Ctrl+C) to leave.
"""

import argparse
import json
import os
import queue
import random
import select
import socket
import struct
import sys
import threading
import time

# ---- Protocol: 4-byte big-endian length prefix + UTF-8 JSON ----------------


def make_message(msg_type, **fields):
    return {"msg_type": msg_type, **fields, "timestamp": int(time.time())}


def send_message(sock, msg):
    data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_exact(sock, n):
    """Read exactly n bytes (handles TCP fragmentation). None means EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_message(sock):
    """Read one framed message. None on EOF; ValueError if the JSON is bad."""
    header = recv_exact(sock, 4)
    payload = (
        recv_exact(sock, struct.unpack("!I", header)[0]) if header else None
    )
    return json.loads(payload.decode("utf-8")) if payload is not None else None


# ---- Board helpers -------------------------------------------------------

SHIPS = {
    "Carrier": 5,
    "Battleship": 4,
    "Cruiser": 3,
    "Submarine": 3,
    "Destroyer": 2,
}
ROWS = "ABCDEFGHIJ"


class QuitGame(Exception):
    pass


def parse_cell(text):
    """'b7' -> (1, 6); None if invalid."""
    text = text.strip().upper()
    if len(text) < 2 or text[0] not in ROWS or not text[1:].isdecimal():
        return None
    row, col = ROWS.index(text[0]), int(text[1:]) - 1
    return (row, col) if 0 <= col < 10 else None


def ship_cells(row, col, size, horizontal):
    return [
        [row, col + i] if horizontal else [row + i, col] for i in range(size)
    ]


def random_fleet():
    taken, ships = set(), []
    for name, size in SHIPS.items():
        while True:
            horizontal = random.random() < 0.5
            row = random.randrange(10 if horizontal else 11 - size)
            col = random.randrange(11 - size if horizontal else 10)
            cells = ship_cells(row, col, size, horizontal)
            if not any(tuple(c) in taken for c in cells):
                break
        taken.update(tuple(c) for c in cells)
        ships.append({"name": name, "cells": cells})
    return ships


def grid_lines(cell_char):
    lines = ["   " + "".join(f"{c + 1:<3}" for c in range(10))]
    for r in range(10):
        lines.append(
            f"{ROWS[r]}  "
            + "".join(f"{cell_char(r, c):<3}" for c in range(10))
        )
    return lines


# ---- Client --------------------------------------------------------------


class Client:
    def __init__(self, sock):
        self.sock, self.alias, self.foe = sock, None, None
        self.phase = "LOBBY"  # LOBBY -> PLACEMENT -> PLAYING
        self.active = None  # alias of the player whose turn it is
        self.mine = set()  # cells of my ships
        self.shots = {}  # my shots at the opponent: (row, col) -> "X" or "o"
        self.incoming = {}  # opponent's shots at me
        self.sunk = []  # enemy ships I have sunk
        self.lines = queue.Queue()  # lines typed while a prompt is open
        self.prompting = False
        threading.Thread(target=self.read_keyboard, daemon=True).start()

    def send(self, msg_type, **fields):
        send_message(
            self.sock, make_message(msg_type, player_id=self.alias, **fields)
        )

    # ---- Keyboard: input is only used while a prompt is open ----

    def read_keyboard(self):
        """Background thread. A line typed with no prompt open is discarded."""
        while True:
            line = sys.stdin.readline()
            text = (
                line.strip() if line else "quit"
            )  # end of input counts as quit
            if self.prompting:
                self.lines.put(text)
            elif text.lower() in ("quit", "exit"):
                print("Leaving the game...")
                try:
                    self.send("DISCONNECT", reason="QUIT")
                except OSError:
                    pass
                sys.stdout.flush()
                os._exit(0)
            elif self.phase == "PLAYING":
                print(
                    "  It's not your turn - input ignored. "
                    f"Waiting for {self.active}'s move..."
                )
            else:
                print(
                    "  Please wait for the game to continue - input ignored."
                )
            if not line:
                return

    def ask(self, prompt):
        """Show a prompt and return the answer ("quit" leaves the game)."""
        while not self.lines.empty():  # drop anything typed before this prompt
            self.lines.get_nowait()
        self.prompting = True
        print(prompt, end="", flush=True)
        text = None
        while text is None:
            try:
                text = self.lines.get(
                    timeout=0.5
                )  # timeout keeps Ctrl+C working
            except queue.Empty:
                pass
        self.prompting = False
        if text.lower() in ("quit", "exit"):
            raise QuitGame
        return text

    def run(self):
        self.send("CONNECT")
        print("Connected. Waiting for the game to start...")
        while True:
            msg = recv_message(self.sock)
            if msg is None:
                raise ConnectionError("The server closed the connection.")
            kind = msg["msg_type"]
            if kind == "LOBBY_WAIT":
                print(msg["message"])
            elif kind == "GAME_START":
                self.foe = msg["opponent_id"]
                print(
                    f"\nYou are {msg['your_role']} playing against {self.foe}."
                )
                self.place_ships()
            elif kind == "STATE_UPDATE":
                self.on_update(msg)
            elif kind == "ERROR":
                print(
                    f"\n[Server error] {msg['error_code']}: {msg['message']}"
                )
                if self.phase == "LOBBY":
                    return 1
                if self.phase == "PLACEMENT":
                    self.place_ships()
                elif self.active == self.alias:
                    self.take_turn()
            elif kind == "GAME_OVER":
                self.game_over(msg)
                return 0

    def on_update(self, msg):
        shooter, self.active, self.phase = (
            self.active,
            msg["active_player"],
            "PLAYING",
        )
        events = ["Both fleets are placed. Let the battle begin!"]
        if msg["result"]:
            row, col = msg["cell"]["row"], msg["cell"]["col"]
            mine = shooter == self.alias
            (self.shots if mine else self.incoming)[(row, col)] = (
                "o" if msg["result"] == "MISS" else "X"
            )
            who = "You" if mine else self.foe
            events = [f"{who} fired at {ROWS[row]}{col + 1}: {msg['result']}"]
            if msg["sunk_ship"]:
                events.append(
                    f"You sank their {msg['sunk_ship']}!"
                    if mine
                    else f"{self.foe} sank your {msg['sunk_ship']}!"
                )
                if mine:
                    self.sunk.append(msg["sunk_ship"])
        self.show(events)
        if self.server_has_more():  # e.g. GAME_OVER follows this update
            return
        again = msg["result"] in ("HIT", "SUNK") and self.active == shooter
        if self.active == self.alias:
            if again:
                print("\nThat was a hit, so you go again!")
            self.take_turn()
        else:
            if again:
                print(f"\nThat was a hit, so {self.foe} goes again.")
            print(f"Waiting for {self.active}'s move...")

    def server_has_more(self):
        """True if the server has already sent another message."""
        return bool(select.select([self.sock], [], [], 0.1)[0])

    def own_char(self, r, c):
        return self.incoming.get((r, c)) or (
            "S" if (r, c) in self.mine else "~"
        )

    def show(self, events):
        print("\n" + "-" * 66)
        print("\n".join(events))
        print(f"\n{'YOUR FLEET':<34}OPPONENT ({self.foe})")
        left = grid_lines(self.own_char)
        right = grid_lines(lambda r, c: self.shots.get((r, c), "~"))
        for a, b in zip(left, right):
            print(f"{a:<34}{b}")
        hits = list(self.shots.values()).count("X")
        print(
            f"\nYour shots: {len(self.shots)} | Hits: {hits} | "
            f"Enemy ships sunk: {len(self.sunk)}/5"
        )
        print("X = hit, o = miss, S = your ship, ~ = water/unknown")

    def take_turn(self):
        while True:
            cell = parse_cell(
                self.ask("\n>>> Your Turn - enter a target cell (e.g. B7): ")
            )
            if cell and cell not in self.shots:
                break
            print("  Invalid cell, or already fired there (use A-J, 1-10).")
        self.send("MOVE", payload={"row": cell[0], "col": cell[1]})

    def place_ships(self):
        self.phase = "PLACEMENT"
        print("\nShips: " + ", ".join(f"{n} ({s})" for n, s in SHIPS.items()))
        if (
            self.ask(
                "Press Enter to place ships yourself, or R for random: "
            ).lower()
            == "r"
        ):
            ships = random_fleet()
            self.mine = {tuple(c) for s in ships for c in s["cells"]}
        else:
            ships = self.place_manually()
        self.send("PLACE_SHIPS", payload={"ships": ships})
        print("Fleet ready. Waiting for your opponent to place their ships...")

    def place_manually(self):
        ships, self.mine = [], set()
        for name, size in SHIPS.items():
            while True:
                print("\n" + "\n".join(grid_lines(self.own_char)))
                parts = self.ask(
                    f"Place {name} ({size}): start cell and H/V (Horizontal/Vertical), e.g. B3 H: "
                ).split()
                start = parse_cell(parts[0]) if len(parts) == 2 else None
                if not start or parts[1].upper() not in ("H", "V"):
                    print("  Format: <cell> <H or V>, e.g. B3 H")
                    continue
                cells = ship_cells(
                    start[0], start[1], size, parts[1].upper() == "H"
                )
                if any(max(c) > 9 for c in cells):
                    print("  That ship would go off the board.")
                elif any(tuple(c) in self.mine for c in cells):
                    print("  That overlaps another ship.")
                else:
                    break
            self.mine.update(tuple(c) for c in cells)
            ships.append({"name": name, "cells": cells})
        return ships

    def game_over(self, msg):
        if msg["outcome"] == "DRAW":
            text = (
                "It's a draw - both fleets were destroyed in the same round."
            )
        elif msg["outcome"] == "FORFEIT":
            text = "Your opponent disconnected. You win by forfeit!"
        else:
            text = "You win!" if msg["winner"] == self.alias else "You lose."
        print(f"\n=== GAME OVER: {text} ===")
        for alias, s in msg["stats"].items():
            name = f"{alias} (you)" if alias == self.alias else alias
            sunk = ", ".join(s["ships_sunk"]) or "none"
            print(
                f"  {name}: {s['shots']} shots, {s['hits']} hits, "
                f"ships sunk: {sunk}"
            )


def main():
    parser = argparse.ArgumentParser(description="Battleship client")
    parser.add_argument(
        "-i",
        dest="host",
        required=True,
        help="server DNS hostname (e.g. server.smith.edu)",
    )
    parser.add_argument(
        "-p", dest="port", type=int, required=True, help="server port"
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")

    try:
        server_ip = socket.gethostbyname(args.host)
    except (socket.gaierror, UnicodeError):
        print(f"[ERROR] Unable to resolve DNS hostname '{args.host}'.")
        print(
            "Please check your Cisco router DNS configuration "
            "('ip dns server' / 'ip host') or verify domain spelling."
        )
        sys.exit(1)
    try:
        sock = socket.create_connection((server_ip, args.port), timeout=10)
    except OSError as exc:
        print(
            f"[ERROR] Could not connect to {args.host} ({server_ip}) "
            f"on port {args.port}: {exc}"
        )
        sys.exit(1)
    sock.settimeout(None)

    client = None
    try:
        client = Client(sock)
        client.alias = client.ask("Enter your player name: ") or "Player"
        return client.run()
    except (QuitGame, KeyboardInterrupt):
        print("\nLeaving the game...")
        if client and client.alias:
            try:
                client.send("DISCONNECT", reason="QUIT")
            except OSError:
                pass
        return 0
    except (
        OSError,
        ValueError,
    ) as exc:  # lost connection or garbage from the server
        print(f"\n[ERROR] Connection problem: {exc}")
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the keyboard thread may still be waiting on input