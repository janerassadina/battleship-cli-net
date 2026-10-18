# Game Behavior & Server Concurrency Architecture Design

## 1. Server Concurrency Model: Multi-Threading
The server is multi-threaded, with one thread per connected client.
- The main thread binds the server socket and loops on `accept()`
- Each accepted client gets a dedicated worker thread (`threading.Thread`). That thread loops on `recv_message(sock)` and passes each message to the game logic.

This fits Battleship for a few reasons because each player gets their own thread. While one thread waits for Player 1's move, the other can still receive Player 2's messages (such as a `DISCONNECT` or an out-of-turn `MOVE`), so one player never blocks the other.

## 2. Thread Safety & Lock Acquisition Strategy 
Both client threads read and change the same game state: the boards, whose turn it is, the stats, and the client sockets. A single `threading.Lock` protects all the shared state so the threads can't interfere with each other.

- Reading a message from the socket happens outside the lock, so a slow player never blocks the other thread.
- Handling a message happens inside the lock from start to finish. That includes the turn check, move validation, board and stats updates, win/draw check, and sending the `STATE_UPDATE` (or `ERROR`) messages. Because all of this happens in one locked block, two moves can never be processed at the same time.
- Disconnect handling takes the same lock and checks a `game_over` flag, so `GAME_OVER` (FORFEIT) is only sent once even if both threads notice a disconnect at the same time.

With only one lock, deadlock between locks can't happen. If a send fails inside the locked block, the error is caught and noted, and the disconnect is handled after the lock is released. That way a thread never tries to take a lock it already holds. 

## 3. Client State & Turn Synchronization Scheme
```mermaid
sequenceDiagram
    autonumber
    actor Client1 as Client 1 (Player 1)
    participant Server as Game Server
    actor Client2 as Client 2 (Player 2)

    Client1->>Server: MOVE (row: 0, col: 2)
    Note over Server: Set game_lock<br>Server validates: Is it Client 1's turn?<br>Is cell (0,2) valid and not guessed yet?
    alt Out of Turn Move / Invalid
        Server-->>Client1: ERROR (OUT_OF_TURN_MOVE, INVALID_COORDINATES, or CELL_ALREADY_GUESSED)
    else Valid Move: HIT or SUNK
        Note over Server: Update boards and stats<br>Record sunk ship if any<br>Player 1 goes again
        Server-->>Client1: STATE_UPDATE (HIT, Turn: Player 1)
        Server-->>Client2: STATE_UPDATE (HIT, Turn: Player 1)
    else Valid Move: MISS
        Note over Server: Update boards and stats<br>Switch active turn to Player 2
        Server-->>Client1: STATE_UPDATE (MISS, Turn: Player 2)
        Server-->>Client2: STATE_UPDATE (MISS, Turn: Player 2)
    end
    Note over Server: If the move decides the game, the server also<br>sends GAME_OVER (outcome, stats) to both clients
    Note over Server: Release game_lock
```