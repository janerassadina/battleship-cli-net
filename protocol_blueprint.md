# Application-Layer Protocol Blueprint

## 1. Transport Layer & Packet Framing Mechanism
- **Transport Protocol:** TCP
- **Serialization Format:** Structured JSON
- **Framing Rule Requirement:** Length-Prefixed Framing (4-byte unsigned integer, Network Byte Order / Big-Endian)

### 1.1 Framing Rule:
Every message is preceded by a 4-byte header that specifies the exact byte length of the payload that follows. The receiver first reads exactly the 4-byte header to determine payload size N, and then reads exactly N bytes (the payload) from the stream before parsing. 

### 1.2 Wire Stream Example:
[4-Byte Length: 0x00000045 (69 bytes)] 
{"msg_type":"CONNECT","player_id":"Alice","timestamp":1727000000}
[4-Byte Length: 0x0000005C (92 bytes)]
{"msg_type":"MOVE","player_id":"Alice","payload":{"row":0,"col":2},"timestamp":1727000005}

### 1.3 Reference Implementation (Python):
```python
import struct
import json

# SENDER: Pack 4-byte big-endian length prefix + UTF-8 payload
payload_bytes = json.dumps(message_dict).encode("utf-8")
header = struct.pack("!I", len(payload_bytes))  # '!I' = 4-byte unsigned big-endian int
sock.sendall(header + payload_bytes)

# RECEIVER: Read exact N bytes loop (to handle TCP stream fragmentation)
def recv_exact(sock, n_bytes):
    buf = bytearray()
    while len(buf) < n_bytes:
        chunk = sock.recv(n_bytes - len(buf))
        if not chunk:
            return None  # Connection closed (EOF)
        buf.extend(chunk)
    return bytes(buf)

# RECEIVER: Process message
header_bytes = recv_exact(sock, 4)
if header_bytes:
    payload_len = struct.unpack("!I", header_bytes)[0]
    payload_bytes = recv_exact(sock, payload_len)
    message = json.loads(payload_bytes.decode("utf-8"))
```

## 2 Application Message Types
| Message Type | Direction | Purpose & Description |
| ------------ | --------- | --------------------- |
| CONNECT | Client -> Server | Client requests to join game room with player alias. |
| LOBBY_WAIT | Server -> Client | Server notifies Client 1 that it is waiting for Player 2 to connect. |
| GAME_START | Server -> Clients | Server assigns roles and signals the ship-placement phase begins. |
| PLACE_SHIPS | Client -> Server | Client submits their fleet layout (ship positions) during setup |
| MOVE | Client -> Server | Active player submits a target coordinate to fire on. |
| STATE_UPDATE | Server -> Clients | Server broadcasts the results of the last move and whose turn is active. |
| ERROR | Server -> Client | Server notifies client of out-of-turn move, invalid coordinates, or malformed message. |
| DISCONNECT | Client -> Server | Client notifies server of intentional departure/quit. |
| GAME_OVER | Server -> Clients | Server broadcasts final game outcome (Winner / Draw / Forfeit).|

### 2.1 Message Schemas:
#### Field Data Types
| Message | Fields (data type) |
| ------- | ------------------ |
| CONNECT | player_id (string) |
| LOBBY_WAIT | message (string) |
| GAME_START | your_role (string), opponent_id (string), phase (string) | 
| PLACE_SHIPS | player_id (string), payload.ships (array of {name: string, cells: [[int,int],...]}, rows/cols 0–9) | 
| MOVE | player_id (string), payload.row (int 0–9), payload.col (int 0–9) | 
| STATE_UPDATE | result (string: HIT/MISS/SUNK), cell ({row: int, col: int}), sunk_ship (string or null), active_player (string), note (string) | 
| ERROR | error_code (string), message (string) | 
| DISCONNECT | player_id (string), reason (string) | 
| GAME_OVER | outcome (string), winner (string or null), reason (string) | 

Every message also has msg_type (string) and timestamp (int)

#### CONNECT 
```json
{
    "msg_type": "CONNECT",
    "player_id": "Alice", 
    "timestamp": 1727000000
}
```

#### LOBBY_WAIT 
```json
{
    "msg_type": "LOBBY_WAIT",
    "message": "Waiting for Player 2 to connect...",
    "timestamp": 1727000001
}
```

#### GAME_START
```json
{
    "msg_type": "GAME_START",
    "your_role": "Player_1",
    "opponent_id": "Bob",
    "phase": "SHIP_PLACEMENT",
    "timestamp": 1727000002
}
```

#### PLACE_SHIPS
```json
{
    "msg_type": "PLACE_SHIPS",
    "player_id": "Alice",
    "payload": {
        "ships": [
            {"name": "Carrier", "cells": [[0,0],[0,1],[0,2],[0,3],[0,4]]},
            {"name": "Battleship", "cells": [[2,0],[2,1],[2,2],[2,3]]},
            {"name": "Cruiser", "cells": [[4,0],[4,1],[4,2]]},
            {"name": "Submarine", "cells": [[6,0],[6,1],[6,2]]},
            {"name": "Destroyer", "cells": [[8,0],[8,1]]}
        ]
    },
    "timestamp": 1727000003
}
```

#### MOVE
```json
{
    "msg_type": "MOVE",
    "player_id": "Alice",
    "payload": {
        "row": 0,
        "col": 2
    },
    "timestamp": 1727000004
}
```

#### STATE_UPDATE
```json
{
    "msg_type": "STATE_UPDATE",
    "result": "HIT",
    "cell": {
        "row": 0,
        "col": 2
    },
    "sunk_ship": null,
    "active_player": "Alice",
    "note": "Hit grants another turn",
    "timestamp": 1727000005
}
```
result is either HIT, MISS, or SUNK. When result is SUNK, sunk_ship names the destroyed ship (e.g. "Destroyer"). active_player stays the same after HIT/SUNK and switches to the opponent after MISS.

#### ERROR
```json
{
    "msg_type": "ERROR",
    "error_code": "OUT_OF_TURN_MOVE",
    "message": "It is not your turn",
    "timestamp": 1727000006
}
```
error_code values: OUT_OF_TURN_MOVE, INVALID_COORDINATES, MALFORMED_MESSAGE, CELL_ALREADY_GUESSED

#### DISCONNECT
```json
{
    "msg_type": "DISCONNECT",
    "player_id": "Alice",
    "reason": "QUIT",
    "timestamp": 1727000007
}
```

#### GAME_OVER
```json
{
    "msg_type": "GAME_OVER",
    "outcome": "WIN",
    "winner": "Alice",
    "reason": "ALL_SHIPS_SUNK",
    "timestamp": 1727000008
}
```
outcome is either WIN, DRAW, or FORFEIT. winner is null when outcome is DRAW. reason is either ALL_SHIPS_SUNK, MUTUAL_SINK (tie case, when both players sink the ships in the same round), OPPONENT_DISCONNECTED

## 3 Connection Termination
| Case | How the server detects it | Server response |
| ---- | ------------------------- | --------------- |
| Graceful (app layer) | Receives a DISCONNECT message | Sends GAME_OVER (FORFEIT) to the opponent, closes the socket |
| Graceful (TCP FIN) | recv() returns b"" (0 bytes = EOF) | same as above|
| Abrupt (crash, RST, network drops) | Catches ConnectionResetError, BrokenPipeError, ConnectionAbortedError, or a TimeoutError | Same as above |

- In every case, the opponent gets GAME_OVER with outcome = "FORFEIT" and reason = "OPPONENT_DISCONNECTED".
- The receive loop must treat a 0-byte read (b"") as EOF and stop reading, otherwise recv() would keep returning
   b"" immediately and the loop spins at 100% CPU
- Example of how the receiver handles both cases: 
```python
    try:
        msg = recv_message(sock)
        if msg is None:
            handle_disconnect(player_id)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError) as e:
        logger.warning(f"Connection lost abruptly: {e}")
        handle_disconnect(player_id)
```