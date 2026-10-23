#!/usr/bin/env python3
"""Real-time bughouse harness for Fairy-Stockfish (UCI).

Two boards, A and B, run concurrently on wall clocks. Team 1 = A-white + B-black,
team 2 = A-black + B-white. A piece captured on one board is added (same colour)
to the holdings on the other board, so it goes to the capturer's partner.
Each player is a separate engine process with its own UCI_Variant (bughouse
or crazyhouse), which lets us compare the two modes.

Game ends on: no legal moves for side to move (treated as loss; bughouse
stalemate is a loss too), flag fall, or illegal move.
"""
import json, re, subprocess, sys, threading, time

ENGINE = sys.argv[1]
OUT = sys.argv[2]
NGAMES = int(sys.argv[3])
BASE_MS = int(sys.argv[4])          # per-player clock
T1_VARIANT, T2_VARIANT = sys.argv[5], sys.argv[6]

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR[] w KQkq - 0 1"


class Engine:
    def __init__(self, variant):
        self.p = subprocess.Popen([ENGINE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, bufsize=1)
        self.send("uci"); self.wait("uciok")
        self.send(f"setoption name UCI_Variant value {variant}")
        self.send("setoption name Threads value 1")
        self.send("setoption name Hash value 64")
        self.send("isready"); self.wait("readyok")

    def send(self, s):
        self.p.stdin.write(s + "\n"); self.p.stdin.flush()

    def wait(self, prefix):
        lines = []
        while True:
            l = self.p.stdout.readline()
            if not l:
                raise RuntimeError("engine died")
            l = l.rstrip("\n"); lines.append(l)
            if l.startswith(prefix):
                return l, lines

    def newgame(self):
        self.send("ucinewgame"); self.send("isready"); self.wait("readyok")

    def apply(self, fen, move):
        """Referee: return FEN after move ('' move = just normalise)."""
        self.send(f"position fen {fen}" + (f" moves {move}" if move else ""))
        self.send("d")
        l, _ = self.wait("Fen:")
        self.wait("Chased:")
        return l.split("Fen: ", 1)[1].strip()

    def has_moves(self, fen):
        self.send(f"position fen {fen}")
        self.send("go perft 1")
        l, _ = self.wait("Nodes searched")
        return int(l.split(":")[1]) > 0

    def quit(self):
        try:
            self.send("quit"); self.p.wait(2)
        except Exception:
            self.p.kill()


def board_pieces(fen):
    """Multiset of pieces on board; promoted pieces (X~) count as pawns."""
    b = fen.split("[")[0]
    out = {}
    i = 0
    while i < len(b):
        c = b[i]
        if c.isalpha():
            promoted = i + 1 < len(b) and b[i + 1] == "~"
            k = ("P" if c.isupper() else "p") if promoted else c
            out[k] = out.get(k, 0) + 1
        i += 1
    return out


def holdings(fen):
    return fen.split("[")[1].split("]")[0]


def add_holding(fen, piece):
    pre, rest = fen.split("[", 1)
    h, post = rest.split("]", 1)
    return f"{pre}[{h}{piece}]{post}"


class Game:
    def __init__(self, players, base_ms, gid, log):
        # players[(board, colour)] -> (Engine, team)
        self.players = players
        self.fen = {"A": START, "B": START}
        self.clock = {(b, c): base_ms for b in "AB" for c in "wb"}
        self.turn_start = {"A": time.monotonic(), "B": time.monotonic()}
        self.lock = threading.Lock()
        self.over = threading.Event()
        self.result = None
        self.moves = {"A": [], "B": []}
        self.refs = {"A": Engine("bughouse"), "B": Engine("bughouse")}
        self.gid, self.log = gid, log
        self.stats = {"captures_passed": 0, "drops": 0, "illegal": 0}

    def finish(self, loser_board, loser_colour, reason):
        with self.lock:
            if self.result:
                return
            team = self.players[(loser_board, loser_colour)][1]
            self.result = {"winner": 3 - team, "reason": reason,
                           "board": loser_board, "loser_colour": loser_colour}
        self.over.set()

    def run_board(self, bd):
        other = "B" if bd == "A" else "A"
        ref = self.refs[bd]
        while not self.over.is_set():
            with self.lock:
                fen = self.fen[bd]
            stm = fen.split()[1]
            if not ref.has_moves(fen):
                self.finish(bd, stm, "checkmate/stalemate (no legal moves)")
                return
            eng, _ = self.players[(bd, stm)]
            t0 = time.monotonic()
            wt, bt = self.clock[(bd, "w")], self.clock[(bd, "b")]
            eng.send(f"position fen {fen}")
            eng.send(f"go wtime {max(wt,1)} btime {max(bt,1)}")
            l, lines = eng.wait("bestmove")
            used = int((time.monotonic() - t0) * 1000)
            self.clock[(bd, stm)] -= used
            if self.clock[(bd, stm)] <= 0:
                self.finish(bd, stm, "flag")
                return
            mv = l.split()[1]
            score = None
            for x in reversed(lines):
                m = re.search(r"score (cp|mate) (-?\d+)", x)
                if m:
                    score = f"{m.group(1)} {m.group(2)}"; break
            with self.lock:
                cur = self.fen[bd]            # may have gained holdings meanwhile
                new = ref.apply(cur, mv)
                if new.split()[:1] == cur.split()[:1] and new.split()[1] == cur.split()[1]:
                    self.stats["illegal"] += 1
                    self.log.write(json.dumps({"g": self.gid, "illegal": mv, "fen": cur}) + "\n")
                    illegal = True
                else:
                    illegal = False
                    before, after = board_pieces(cur), board_pieces(new)
                    # pieces of the opponent that vanished = captures
                    opp_lower = stm == "w"
                    for k, n in before.items():
                        if k.islower() == opp_lower and after.get(k, 0) < n:
                            for _ in range(n - after.get(k, 0)):
                                self.fen[other] = add_holding(self.fen[other], k)
                                self.stats["captures_passed"] += 1
                    if "@" in mv:
                        self.stats["drops"] += 1
                    self.fen[bd] = new
                    self.moves[bd].append({"m": mv, "c": stm, "score": score, "ms": used})
            if illegal:
                self.finish(bd, stm, f"illegal move {mv}")
                return

    def play(self):
        for (eng, _) in self.players.values():
            eng.newgame()
        ths = [threading.Thread(target=self.run_board, args=(b,)) for b in "AB"]
        for t in ths: t.start()
        # flag watchdog for side to move
        while not self.over.wait(0.05):
            pass
        for (eng, _) in self.players.values():
            eng.send("stop")
        for t in ths: t.join(5)
        for r in self.refs.values(): r.quit()
        return self.result


def main():
    log = open(OUT, "w")
    tally = {1: 0, 2: 0}
    reasons = {}
    for g in range(NGAMES):
        swap = g % 2 == 1         # alternate which team sits on A-white
        v1, v2 = T1_VARIANT, T2_VARIANT
        engines = {k: Engine(v) for k, v in
                   {("A", "w"): v1, ("B", "b"): v1, ("A", "b"): v2, ("B", "w"): v2}.items()}
        if swap:
            players = {("A", "w"): (engines[("A", "b")], 2), ("B", "b"): (engines[("B", "w")], 2),
                       ("A", "b"): (engines[("A", "w")], 1), ("B", "w"): (engines[("B", "b")], 1)}
        else:
            players = {k: (e, 1 if k in [("A", "w"), ("B", "b")] else 2) for k, e in engines.items()}
        game = Game(players, BASE_MS, g, log)
        t0 = time.time()
        res = game.play()
        tally[res["winner"]] += 1
        reasons[res["reason"].split()[0]] = reasons.get(res["reason"].split()[0], 0) + 1
        rec = {"g": g, "team1": v1, "team2": v2, "result": res, "secs": round(time.time() - t0, 1),
               "plies": {b: len(game.moves[b]) for b in "AB"}, "stats": game.stats,
               "final_fen": game.fen, "moves": game.moves}
        log.write(json.dumps(rec) + "\n"); log.flush()
        print(f"game {g}: team{res['winner']} ({v1 if res['winner']==1 else v2}) wins by "
              f"{res['reason']} on board {res['board']}; plies A={len(game.moves['A'])} "
              f"B={len(game.moves['B'])} drops={game.stats['drops']} passed={game.stats['captures_passed']} "
              f"illegal={game.stats['illegal']} {rec['secs']}s", flush=True)
        for e in engines.values(): e.quit()
    print(f"TOTAL team1[{T1_VARIANT}]={tally[1]} team2[{T2_VARIANT}]={tally[2]} reasons={reasons}")


if __name__ == "__main__":
    main()
