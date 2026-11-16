#!/usr/bin/env python3
"""Battleship server: one thread per client, one game_lock for all state.

Usage: python3 server.py -p 8080
"""

import argparse
import json
import logging
import socket
import struct
import threading
import time

log = logging.getLogger("server")

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


REQUIRED = {  # fields each client message must have
    "CONNECT": {"player_id": str},
    "PLACE_SHIPS": {"player_id": str, "payload": dict},
    "MOVE": {"player_id": str, "payload": dict},
    "DISCONNECT": {"player_id": str, "reason": str},
}


def schema_problem(msg):
    """Return a reason string if msg doesn't match the schema, else None."""
    kind = msg.get("msg_type") if isinstance(msg, dict) else None
    if not isinstance(kind, str) or kind not in REQUIRED:
        return "unknown or missing msg_type"
    for field, field_type in REQUIRED[kind].items():
        if not isinstance(msg.get(field), field_type):
            return f"{kind} needs '{field}' of type {field_type.__name__}"
    return None


# ---- Game rules ----------------------------------------------------------

SHIPS = {
    "Carrier": 5,
    "Battleship": 4,
    "Cruiser": 3,
    "Submarine": 3,
    "Destroyer": 2,
}


def fleet_problem(ships):
    """Return None for a legal fleet, else a short reason."""
    try:
        if sorted(s["name"] for s in ships) != sorted(SHIPS):
            return "fleet must have exactly one of each ship"
        used = set()
        for ship in ships:
            name, cells = ship["name"], sorted(tuple(c) for c in ship["cells"])
            if len(cells) != SHIPS[name] or any(
                len(c) != 2
                or not all(type(v) is int and 0 <= v < 10 for v in c)
                for c in cells
            ):
                return (
                    f"{name} has the wrong number of cells or is off the board"
                )
            horizontal = len({r for r, _ in cells}) == 1
            line = [c[1] if horizontal else c[0] for c in cells]
            if not (
                horizontal or len({c for _, c in cells}) == 1
            ) or line != list(range(line[0], line[0] + len(line))):
                return f"{name} must be a straight, unbroken line"
            if used & set(cells):
                return f"{name} overlaps another ship"
            used |= set(cells)
    except (KeyError, TypeError, ValueError):
        return "fleet is malformed"
    return None


class Fleet:
    def __init__(self, ships):
        self.ships = {s["name"]: {tuple(c) for c in s["cells"]} for s in ships}
        self.hits = set()

    def fire(self, cell):
        """Return (result, sunk_ship_name_or_None)."""
        for name, cells in self.ships.items():
            if cell in cells:
                self.hits.add(cell)
                return ("SUNK", name) if cells <= self.hits else ("HIT", None)
        return "MISS", None

    def all_sunk(self):
        return all(cells <= self.hits for cells in self.ships.values())


# ---- Game state and FSM --------------------------------------------------


class Player:
    def __init__(self, sock):
        self.sock, self.alias, self.fleet = sock, None, None
        self.guesses, self.gone = set(), False
        self.stats = {"shots": 0, "hits": 0, "ships_sunk": []}


class Game:
    def __init__(self):
        self.players = []  # players[0] is Player_1, players[1] is Player_2
        self.state = "WAITING_FOR_PLAYERS"
        self.active = 0  # index of the player whose turn it is
        self.over = False
        self.failed = []  # players whose socket failed during a send

    def go(self, state, why):
        log.info("FSM %s -> %s (%s)", self.state, state, why)
        self.state = state

    def role(self, player):
        return f"Player_{self.players.index(player) + 1}"

    def other(self, player):
        return self.players[1 - self.players.index(player)]


class Server:
    def __init__(self):
        self.lock = (
            threading.Lock()
        )  # game_lock: guards self.game and all its state
        self.game = Game()

    def serve(self, port):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", port))
        listener.listen()
        log.info(
            "FSM INIT -> WAITING_FOR_PLAYERS (listening on port %d)", port
        )
        while True:
            conn, _ = listener.accept()
            with self.lock:
                game = self.game
                player = None
                if (
                    len(game.players) < 2
                    and game.state == "WAITING_FOR_PLAYERS"
                ):
                    player = Player(conn)
                    game.players.append(player)
            if (
                player
            ):  # worker thread per client; main thread goes back to accept()
                threading.Thread(
                    target=self.client_loop, args=(game, player), daemon=True
                ).start()
            else:
                try:
                    send_message(
                        conn,
                        make_message(
                            "ERROR",
                            error_code="GAME_FULL",
                            message="Game already has two players.",
                        ),
                    )
                except OSError:
                    pass
                conn.close()

    def client_loop(self, game, player):
        """Read messages outside the lock, handle each one inside it."""
        try:
            while True:
                try:
                    msg = recv_message(player.sock)
                except ValueError:
                    msg = {}  # bad JSON: handle() replies MALFORMED_MESSAGE
                except OSError:  # connection reset, broken pipe, timeout
                    break
                if msg is None:  # 0-byte read = EOF
                    break
                with self.lock:
                    self.handle(game, player, msg)
                    done = game.over or player.gone
                self.drop_failed(game)
                if done:
                    break
        finally:
            with self.lock:
                self.leave(game, player)
            self.drop_failed(game)
            player.sock.close()

    # Everything below runs with game_lock held (except drop_failed).

    def send(self, game, player, msg):
        """A failed send is noted and handled after the lock is released."""
        if player.gone:
            return
        try:
            send_message(player.sock, msg)
        except OSError:
            game.failed.append(player)

    def error(self, game, player, code, text):
        self.send(
            game, player, make_message("ERROR", error_code=code, message=text)
        )

    def update(self, game, result, cell, sunk, next_player):
        msg = make_message(
            "STATE_UPDATE",
            result=result,
            cell=cell,
            sunk_ship=sunk,
            active_player=next_player.alias,
        )
        for p in game.players:  # same update goes to both clients
            self.send(game, p, msg)

    def drop_failed(self, game):
        while True:
            with self.lock:
                if not game.failed:
                    return
                self.leave(game, game.failed.pop(0))

    def handle(self, game, player, msg):
        if game.over or player.gone:
            return
        problem = schema_problem(msg)
        if problem:
            self.error(game, player, "MALFORMED_MESSAGE", problem)
        elif msg["msg_type"] == "CONNECT":
            self.connect(game, player, msg["player_id"].strip())
        elif player.alias is None or msg["player_id"] != player.alias:
            self.error(
                game,
                player,
                "MALFORMED_MESSAGE",
                "send CONNECT first, then reuse that player_id",
            )
        elif msg["msg_type"] == "DISCONNECT":
            self.leave(game, player)
        elif msg["msg_type"] == "PLACE_SHIPS":
            self.place(game, player, msg["payload"].get("ships"))
        else:
            self.move(game, player, msg["payload"])

    def connect(self, game, player, alias):
        if player.alias is not None:
            self.error(game, player, "MALFORMED_MESSAGE", "already connected")
            return
        if not alias or any(p.alias == alias for p in game.players):
            self.error(
                game,
                player,
                "MALFORMED_MESSAGE",
                "alias is empty or already in use",
            )
            self.leave(game, player)
            return
        player.alias = alias
        if len(game.players) == 2 and all(p.alias for p in game.players):
            game.go("GAME_START", "2 clients connected")
            for p in game.players:
                self.send(
                    game,
                    p,
                    make_message(
                        "GAME_START",
                        your_role=game.role(p),
                        opponent_id=game.other(p).alias,
                        phase="SHIP_PLACEMENT",
                    ),
                )
            game.go("SHIP_PLACEMENT", "roles assigned")
        else:
            self.send(
                game,
                player,
                make_message(
                    "LOBBY_WAIT", message="Waiting for Player 2 to connect..."
                ),
            )

    def place(self, game, player, ships):
        if game.state != "SHIP_PLACEMENT" or player.fleet:
            self.error(
                game,
                player,
                "MALFORMED_MESSAGE",
                "ships can only be placed once, during setup",
            )
            return
        problem = fleet_problem(ships)
        if problem:
            self.error(game, player, "INVALID_COORDINATES", problem)
            return
        player.fleet = Fleet(ships)
        if all(p.fleet for p in game.players):
            game.active = 0
            game.go("PLAYER_TURN", "both fleets placed")
            self.update(
                game, None, None, None, game.players[0]
            )  # first-turn notice, no shot yet

    def move(self, game, player, payload):
        row, col = payload.get("row"), payload.get("col")
        if (
            game.state not in ("PLAYER_TURN", "PENDING_WIN")
            or game.players[game.active] is not player
        ):
            self.error(
                game, player, "OUT_OF_TURN_MOVE", "It is not your turn."
            )
            return

        before = game.state
        game.go("EVALUATE_MOVE", f"{player.alias} sent a MOVE")
        if type(row) is not int or type(col) is not int:
            problem = ("MALFORMED_MESSAGE", "row and col must be integers")
        elif not (0 <= row < 10 and 0 <= col < 10):
            problem = ("INVALID_COORDINATES", "row and col must be 0-9")
        elif (row, col) in player.guesses:
            problem = ("CELL_ALREADY_GUESSED", "you already fired there")
        else:
            problem = None
        if problem:  # invalid move: ERROR to the sender, same player's turn
            self.error(game, player, *problem)
            game.go(before, "invalid move")
            return

        foe = game.other(player)
        result, sunk = foe.fleet.fire((row, col))
        player.guesses.add((row, col))
        player.stats["shots"] += 1
        if result != "MISS":
            player.stats["hits"] += 1
        if sunk:
            player.stats["ships_sunk"].append(sunk)
        cell = {"row": row, "col": col}

        if foe.fleet.all_sunk():
            if (
                before == "PENDING_WIN"
            ):  # Player 2 also sank everything in the final turn
                self.update(game, result, cell, sunk, player)
                self.finish(game, "DRAW", None, "MUTUAL_SINK")
            elif (
                game.active == 0
            ):  # Player 1 finished first: Player 2 gets one last turn
                game.active = 1
                game.go("PENDING_WIN", "Player 1 sank the last ship")
                self.update(game, result, cell, sunk, game.players[1])
            else:  # Player 2 finished; Player 1 already had this round's turn
                self.update(game, result, cell, sunk, player)
                self.finish(game, "WIN", player, "ALL_SHIPS_SUNK")
        elif (
            result == "MISS" and before == "PENDING_WIN"
        ):  # final turn ended with a miss
            self.update(game, result, cell, sunk, player)
            self.finish(game, "WIN", game.players[0], "ALL_SHIPS_SUNK")
        else:
            if result == "MISS":
                game.active = 1 - game.active  # a miss passes the turn
                nxt = game.players[game.active].alias
                why = f"{result}, turn passes to {nxt}"
            else:  # a hit keeps the turn
                why = f"{result}, {player.alias} goes again"
            game.go(before, why)
            self.update(game, result, cell, sunk, game.players[game.active])

    def finish(self, game, outcome, winner, reason):
        """Send GAME_OVER with both players' stats, then open a fresh lobby."""
        game.go("GAME_OVER", f"{outcome}: {reason}")
        game.over = True
        msg = make_message(
            "GAME_OVER",
            outcome=outcome,
            winner=winner.alias if winner else None,
            reason=reason,
            stats={p.alias: p.stats for p in game.players},
        )
        for p in game.players:
            self.send(game, p, msg)
        game.go("CLEANUP", "result sent")
        self.game = Game()
        log.info("FSM CLEANUP -> WAITING_FOR_PLAYERS (new lobby)")

    def leave(self, game, player):
        """Player quit (DISCONNECT), hit EOF, or the connection dropped."""
        if player.gone:
            return
        player.gone = True
        if game.over:
            return
        if game.state == "WAITING_FOR_PLAYERS":
            game.players.remove(player)
        else:
            self.finish(
                game, "FORFEIT", game.other(player), "OPPONENT_DISCONNECTED"
            )


def main():
    parser = argparse.ArgumentParser(description="Battleship server")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        required=True,
        help="port to listen on (binds to 0.0.0.0)",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        Server().serve(args.port)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        log.error("Cannot listen on port %d: %s", args.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())