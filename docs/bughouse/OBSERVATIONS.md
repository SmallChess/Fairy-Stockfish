# Bughouse support in Fairy-Stockfish: observations

Tested on 2026-10-23 at commit `9f778da` of SmallChess/Fairy-Stockfish, with the engine built by
`make build ARCH=apple-silicon largeboards=yes` on an Apple M2.

## Summary

- `UCI_Variant bughouse` (and `koedem`) are real variants, not just a crazyhouse alias.
- **One engine process plays one seat** (one colour on one board), so a full bughouse game needs
  **four processes** plus something that runs the game (an ICS such as FICS, or a harness like the one
  in this folder). That arbiter moves captured pieces to the partner's board, keeps the clocks and
  relays partner messages.
- In 22 four-engine games played by the harness there were no illegal moves, no piece-accounting
  errors and no crashes, and every game ended in checkmate.

## How it is implemented

`src/variant.cpp`, `bughouse_variant()`:

```cpp
Variant* v = crazyhouse_variant()->init();
v->twoBoards = true;          // enables partner logic and "virtual" drops
v->capturesToHand = false;    // captured pieces go to the partner, not to you
v->stalemateValue = -VALUE_MATE;  // stalemate loses
```

- **Captures leave your board.** After `e2e4 d7d5 e4d5` the FEN holdings stay `[]`. The engine never
  adds a captured piece to anyone's pocket; the arbiter must add it to the partner board's holdings.
  Pieces arrive through the FEN, e.g. `.../RNBQKBNR[Qp] w ...`.
- **Virtual drops** (`movegen.cpp`, `position.cpp`, `search.cpp`). When `twoBoards` is set, the search
  may consider dropping pieces it does not hold yet, i.e. ones the partner might send. Lines such as
  `pv d8d5 Q@a4 ...` appear at low depth from the start position.
  - They are never generated at the root or in perft, so the engine never sends a move it cannot play.
  - A mate that relies on virtual pieces is scored around `VALUE_VIRTUAL_MATE` (3000 internal), not as
    a real mate.
  - If the chosen best move is a virtual drop, the engine waits (xboard mode) for the piece to arrive.
- **Partner protocol, xboard only** (`src/partner.cpp`). The engine emits `tellics ptell <msg>` and
  parses `ptell <msg>`. Commands it understands: `help`, `sit`, `go`, `move [uci]`, `fast`, `slow`,
  `dead`, `x`, `time <cs>`, `otim <cs>`.
  - At game start it sends a `partner` handshake. If the partner is also Fairy-Stockfish, the two
    exchange clock times automatically.
  - Its search sends these to the partner on its own (`search.cpp` ~L555–613):
    - `dead`: it is getting mated.
    - `sit`: it has a mate and the opponent's clock is lower than the partner's.
    - `fast`: it has a virtual win.
    - `x` / `slow`: the previous condition no longer holds.
  - `timeman.cpp` reduces its time budget when the partner reports being dead.
- **UCI mode has no partner channel.** The engine plays its board, assumes nothing about its partner,
  and relies on the arbiter to update holdings between moves.

## Partner-protocol test (xboard, scripted)

| Sent | Engine response |
| --- | --- |
| `partner human` | `tellics ptell partner Fairy-Stockfish is an engine. Ask it 'help' ...` |
| `ptell help` | lists the commands above |
| `ptell sit`, then `go` | `I sit, tell me 'go' to continue`; it did not move until `ptell go` arrived |
| `ptell move e7e5` (not legal for its side) | `sorry, not possible` |
| Position with a winning attack that needs pieces it lacks | while searching it sent `tellics ptell fast` to the partner (virtual win), then played `Q@e8+` |

## Four-engine games (UCI harness)

The harness is `bughouse_match.py` in this folder. It runs four engine processes (`Threads 1`, `Hash 64`)
plus two referee processes. Board A and board B run concurrently on real wall clocks. A captured piece
(a promoted piece counts as a pawn) is added in the same colour to the other board's holdings, i.e. to
the capturer's partner. The game ends when the side to move has no legal moves (`go perft 1` = 0), a
flag falls, or a move is illegal. Colours alternate between games.

```bash
python3 docs/bughouse/bughouse_match.py src/stockfish out.jsonl 10 60000 bughouse crazyhouse
```

Arguments: engine, log file, number of games, per-player clock in milliseconds, team 1 variant,
team 2 variant.

| Match (1 min per player, 0 increment) | Games | Result | Endings | Avg plies (both boards) |
| --- | --- | --- | --- | --- |
| Smoke test, bughouse vs bughouse, 20 s | 2 | 1–1 | 2 checkmates | ~140 |
| `bughouse` team vs `crazyhouse` team | 10 | **5–5** | 10 checkmates | 89 |
| `bughouse` vs `bughouse` | 6 | 4–2 | 6 checkmates | 123 |

In every game: 0 illegal moves, 0 piece-count errors (total pieces on both boards plus holdings = 64),
and 0 flag losses. Each final position was confirmed to have the side to move in check. Games are
drop-heavy: typically 15–40 drops and 20–60 pieces passed between boards per game.

### Interpretation

- Bughouse mode is stable and rules-correct at blitz speed.
- Bughouse mode showed **no measurable edge** over an engine that wrongly believes it is playing
  crazyhouse (5–5). Ten games are far too few to resolve a small difference, and in UCI mode the
  main bughouse features (partner signals, sitting, waiting for virtual drops) are unused, so the two
  modes differ mainly in how they value captures and stalemate.
- To test the coordination logic, the harness needs to drive the engines over **xboard** and relay
  `tellics ptell` lines between partners. That is not done yet.

## Caveats of the harness

- "No legal moves" is treated as mate. Under strict FICS rules a mate that could be blocked by a piece
  the partner may send is not final. Here every final position was a check that could not be blocked
  with the current holdings.
- Holdings that arrive while an engine is thinking are only seen on its next move.
- Short sample sizes; the results are smoke tests, not ratings.
