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

## 2. Client State & Turn Synchronization Scheme

## 3. Score & Leaderboard Synchronization Design