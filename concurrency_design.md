# Game Behavior & Server Concurrency Architecture Design

## 1. Server Concurrency Model: Multi-Threading
The server is multi-threaded, with one thread per connected client.
- The main thread binds the server socket and loops on `accept()`
- Each accepted client gets a dedicated worker thread (`threading.Thread`). That thread loops on `recv_message(sock)` and passes each message to the game logic.

This fits Battleship for a few reasons because each player gets their own thread. While one thread waits for Player 1's move, the other can still receive Player 2's messages (such as a `DISCONNECT` or an out-of-turn `MOVE`), so one player never blocks the other.
