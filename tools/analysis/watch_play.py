#!/usr/bin/env python3
"""
watch_play.py â€” Self-play with live dashboard-mirroring terminal output.

Runs the bot engine against itself (or Stockfish) using the EXACT same search
pipeline as game.js / engine.js:
  â€¢ `go infinite` per move
  â€¢ Only processes lines containing BOTH 'depth' AND 'score'
  â€¢ Confidence-based stop (pv-stability + eval-stability)
  â€¢ Hard maxTimeMs ceiling

Prints every info line live so you can see exactly what the dashboard would
receive â€” and specifically when/why eval_cp goes null (â†’ eval bar snaps to 0).

Usage
-----
  # Single self-play game with ~2s budget per move:
    python tools/watch_play.py

  # 3 games, 1s per move, show board after each move:
    python tools/watch_play.py --games 3 --movetime 1000 --board

  # Play vs Stockfish (bot as White):
    python tools/watch_play.py --p2-sf --movetime 1000

  # Suppress depth-by-depth lines, only show move decisions:
    python tools/watch_play.py --quiet

Options
-------
  --movetime MS       Hard ceiling per move in ms (default: 2000)
  --games N           Number of games to play (default: 1)
  --board             Render ASCII board after each move
  --quiet             Only show move decisions, not every depth line
  --p2-sf             Use Stockfish as player 2 (default: engine vs itself)
  --min-depth N       Minimum depth before confidence stop fires (default: 8)
  --conf-thresh F     Confidence threshold to stop search (default: 0.75)
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# â”€â”€ UTF-8 on Windows â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if sys.platform == "win32":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    try:
        import ctypes as _c
        _c.windll.kernel32.SetConsoleMode(_c.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

_ROOT  = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

from engine_config import find_engine as _find_bot_engine

try:
    import chess
    import chess.pgn
    HAS_CHESS = True
except ImportError:
    HAS_CHESS = False

# â”€â”€ ANSI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _e(code, t): return f"\033[{code}m{t}\033[0m"
def green(t):   return _e("92", t)
def red(t):     return _e("91", t)
def yellow(t):  return _e("93", t)
def cyan(t):    return _e("96", t)
def bold(t):    return _e("1",  t)
def dim(t):     return _e("2",  t)
def magenta(t): return _e("95", t)
def blue(t):    return _e("94", t)

# â”€â”€ Data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@dataclass
class InfoLine:
    depth:    int   = 0
    seldepth: int   = 0
    score_cp: Optional[int]  = None
    score_mate: Optional[int] = None
    nodes:    int   = 0
    nps:      int   = 0
    time_ms:  int   = 0
    pv:       list[str] = field(default_factory=list)

    @property
    def pv0(self) -> Optional[str]:
        return self.pv[0] if self.pv else None

    @property
    def eval_cp(self) -> Optional[int]:
        return self.score_cp

    @property
    def eval_str(self) -> str:
        if self.score_mate is not None:
            m = self.score_mate
            s = f"+M{m}" if m > 0 else f"-M{abs(m)}"
            return (green if m > 0 else red)(s)
        if self.score_cp is not None:
            cp = self.score_cp
            s  = ("+" if cp > 0 else "") + f"{cp/100:.2f}"
            return green(s) if cp >= 50 else (red(s) if cp <= -50 else yellow(s))
        return red("NULL")   # â† this is what makes the eval bar snap to 0


def _parse_info(line: str) -> Optional[InfoLine]:
    """Mirror of engine.js parseInfo â€” only real scored depth lines.
    Excludes 'info string ...' lines even if they contain 'depth'/'score' as words
    (aspiration events, rootmoves, searchdiag etc.) â€” same fix as engine.js."""
    if line.startswith("info string"):
        return None   # diagnostic lines â€” excluded
    if not (line.startswith("info") and "depth" in line and "score" in line):
        return None
    parts = line.split()
    out = InfoLine()

    def _int(s):
        try: return int(s)
        except (ValueError, TypeError): return None

    i = 1
    while i < len(parts):
        t = parts[i]
        if   t == "depth"    and i+1 < len(parts): out.depth    = _int(parts[i+1]) or 0; i += 2
        elif t == "seldepth" and i+1 < len(parts): out.seldepth = _int(parts[i+1]) or 0; i += 2
        elif t == "nodes"    and i+1 < len(parts): out.nodes    = _int(parts[i+1]) or 0; i += 2
        elif t == "nps"      and i+1 < len(parts): out.nps      = _int(parts[i+1]) or 0; i += 2
        elif t == "time"     and i+1 < len(parts): out.time_ms  = _int(parts[i+1]) or 0; i += 2
        elif t == "score"    and i+2 < len(parts):
            kind = parts[i+1]
            val  = _int(parts[i+2])
            if kind == "cp"   and val is not None: out.score_cp   = val; i += 3
            elif kind == "mate" and val is not None: out.score_mate = val; i += 3
            else: i += 1
        elif t == "pv":
            out.pv = parts[i+1:]; break
        else:
            i += 1
    return out if out.depth > 0 else None


# â”€â”€ Confidence (mirrors policies.js computeConfidence) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
K_STABILITY = 30.0
K_PV        = 6.0
ALPHA_CMPLX = 0.25
K_COMPLEX   = 3.0
COMMITTED_STREAK = 7

def _compute_confidence(history: list[InfoLine]) -> float:
    if len(history) < 2:
        return 0.0
    window = history[-5:]
    deltas = []
    for i in range(1, len(window)):
        a, b = window[i].eval_cp, window[i-1].eval_cp
        if a is not None and b is not None:
            deltas.append(abs(a - b))
    avg_delta = sum(deltas) / len(deltas) if deltas else 100.0
    eval_stab = 1.0 / (1.0 + avg_delta / K_STABILITY)

    last_pv0 = history[-1].pv0
    pv_streak = 1
    if last_pv0:
        for i in range(len(history)-2, -1, -1):
            if history[i].pv0 == last_pv0: pv_streak += 1
            else: break
    pv_stab = min(pv_streak / K_PV, 1.0)

    last = history[-1]
    complex_ratio = last.seldepth / last.depth if last.depth > 0 else 2.0
    complex_factor = 1.0 - ALPHA_CMPLX * min(complex_ratio / K_COMPLEX, 1.0)

    return eval_stab * pv_stab * complex_factor


def _committed_streak(history: list[InfoLine]) -> int:
    if not history: return 0
    pv0 = history[-1].pv0
    streak = 0
    for info in reversed(history):
        if info.pv0 == pv0: streak += 1
        else: break
    return streak


# â”€â”€ Engine subprocess â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class Engine:
    _EXTRA_PATH = [r"C:\mingw64\bin"]

    def __init__(self, path: str, threads: int = 1, hash_mb: int = 64, label: str = ""):
        self.label = label or os.path.basename(path)
        self._path = path
        env = os.environ.copy()
        for p in self._EXTRA_PATH:
            if os.path.isdir(p) and p not in env.get("PATH", ""):
                env["PATH"] = p + os.pathsep + env.get("PATH", "")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._p = subprocess.Popen(
            [path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env=env, creationflags=flags,
        )
        self._send("uci")
        while True:
            ln = self._read()
            if "uciok" in ln: break
        self._send(f"setoption name Threads value {threads}")
        self._send(f"setoption name Hash value {hash_mb}")
        self._send("isready")
        while True:
            if "readyok" in self._read(): break

    def _send(self, cmd: str):
        self._p.stdin.write(cmd + "\n")
        self._p.stdin.flush()

    def _read(self) -> str:
        line = self._p.stdout.readline()
        if not line:
            raise RuntimeError(f"Engine {self.label!r} died")
        return line.rstrip("\n\r")

    def new_game(self):
        self._send("ucinewgame")
        self._send("isready")
        while True:
            if "readyok" in self._read(): break

    def search(self, fen: str, moves: list[str], max_time_ms: int,
               min_depth: int, conf_thresh: float,
               quiet: bool = False) -> tuple[str, Optional[str], list[InfoLine]]:
        """
        Mirror of game.js thinkDynamic:
          - `go infinite`
          - Process only lines with BOTH depth AND score
          - Stop when confidence >= threshold OR committed streak >= COMMITTED_STREAK
          - Hard stop at max_time_ms
        Returns (bestmove, ponder, info_history).
        """
        # Set position
        pos = f"position fen {fen}" if fen != "startpos" else "position startpos"
        if moves:
            pos += " moves " + " ".join(moves)
        self._send(pos)

        history: list[InfoLine] = []
        t0 = time.perf_counter()
        self._send("go infinite")

        prev_pv0 = None

        while True:
            line = self._read()
            elapsed = (time.perf_counter() - t0) * 1000

            # â”€â”€ Info line (same filter as engine.js) â”€â”€
            if line.startswith("info"):
                if "string" in line.split()[:2]:
                    # info string lines â€” same ones that caused the parser crash
                    # and that the dashboard never sees. Print dimmed.
                    if not quiet:
                        print(dim(f"  [engine-msg] {line[12:]}"))
                    continue

                info = _parse_info(line)
                if info is None:
                    # Has 'info' but missing depth or score â€” EXACTLY what the
                    # dashboard never receives. Flag it so we can see the gap.
                    if not quiet and "depth" in line:
                        print(yellow(f"  [SKIPPED â€” no score] {line.strip()}"))
                    continue

                history.append(info)
                conf = _compute_confidence(history)
                streak = _committed_streak(history)

                # â”€â”€ Print this depth line â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                pv0_changed = info.pv0 != prev_pv0 and prev_pv0 is not None
                pv_str = " ".join(info.pv[:5]) if info.pv else "â€“"
                nps_str = f"{info.nps/1e6:.2f}M" if info.nps >= 1_000_000 else f"{info.nps//1000}K"
                if not quiet:
                    marker = magenta(" â† pv change!") if pv0_changed else ""
                    print(
                        f"  d{info.depth:02d}/{info.seldepth:<2d}  "
                        f"eval={info.eval_str:<22s}  "
                        f"conf={cyan(f'{conf:.2f}')}  "
                        f"streak={streak:2d}  "
                        f"nps={dim(nps_str)}  "
                        f"{dim(pv_str)}"
                        f"{marker}"
                    )
                prev_pv0 = info.pv0

                # â”€â”€ Stop conditions (mirrors policies.js shouldStopSearch) â”€â”€
                if elapsed >= max_time_ms:
                    if not quiet:
                        print(yellow(f"  â†’ STOP: time ceiling {max_time_ms}ms hit"))
                    self._send("stop")
                    break
                if info.depth >= min_depth:
                    if conf >= conf_thresh:
                        if not quiet:
                            print(green(f"  â†’ STOP: confidence {conf:.3f} >= {conf_thresh}"))
                        self._send("stop")
                        break
                    if streak >= COMMITTED_STREAK:
                        if not quiet:
                            print(green(f"  â†’ STOP: committed streak {streak} depths"))
                        self._send("stop")
                        break

            elif line.startswith("bestmove"):
                parts = line.split()
                bestmove = parts[1] if len(parts) > 1 else None
                ponder   = parts[3] if len(parts) > 3 and parts[2] == "ponder" else None
                return bestmove, ponder, history

        # Drain until bestmove (after sending stop)
        while True:
            ln = self._read()
            if ln.startswith("bestmove"):
                parts = ln.split()
                bestmove = parts[1] if len(parts) > 1 else None
                ponder   = parts[3] if len(parts) > 3 and parts[2] == "ponder" else None
                return bestmove, ponder, history

    def close(self):
        try:
            self._send("quit")
            self._p.wait(timeout=3)
        except Exception:
            self._p.kill()


# â”€â”€ Board rendering â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _render_board(fen: str, last_move: Optional[str] = None) -> str:
    if not HAS_CHESS:
        return f"  FEN: {fen}"
    board = chess.Board(fen)
    lm = chess.Move.from_uci(last_move) if last_move else None
    LIGHT = "\033[48;5;223m"; DARK = "\033[48;5;130m"; HL = "\033[48;5;184m"
    RST = "\033[0m"; WP = "\033[97m"; BP = "\033[30m"
    UNI = {"P":"â™™","N":"â™˜","B":"â™—","R":"â™–","Q":"â™•","K":"â™”",
           "p":"â™Ÿ","n":"â™ž","b":"â™","r":"â™œ","q":"â™›","k":"â™š"}
    hl = set()
    if lm: hl.add(lm.from_square); hl.add(lm.to_square)
    lines = []
    for r in range(7, -1, -1):
        row = f" {r+1} "
        for f in range(8):
            sq = chess.square(f, r)
            bg = HL if sq in hl else (LIGHT if (r+f)%2==1 else DARK)
            pc = board.piece_at(sq)
            if pc:
                sym = UNI.get(pc.symbol(), pc.symbol())
                fg = WP if pc.color == chess.WHITE else BP
                row += f"{bg}{fg} {sym} {RST}"
            else:
                row += f"{bg}   {RST}"
        lines.append(row)
    lines.append("   " + "  ".join(" abcdefgh"[f+1] for f in range(8)))
    return "\n".join(lines)


def _apply_moves(fen: str, moves: list[str]) -> str:
    if not HAS_CHESS:
        return fen
    board = chess.Board(fen)
    for uci in moves:
        board.push(chess.Move.from_uci(uci))
    return board.fen()


def _is_game_over(fen: str, moves: list[str]) -> tuple[bool, str]:
    if not HAS_CHESS:
        return False, ""
    board = chess.Board(fen)
    for uci in moves:
        board.push(chess.Move.from_uci(uci))
    if board.is_checkmate():
        winner = "Black" if board.turn == chess.WHITE else "White"
        return True, f"Checkmate â€” {winner} wins"
    if board.is_stalemate():    return True, "Stalemate â€” Draw"
    if board.is_insufficient_material(): return True, "Insufficient material â€” Draw"
    if board.is_seventyfive_moves():     return True, "75-move rule â€” Draw"
    if board.is_fivefold_repetition():   return True, "Fivefold repetition â€” Draw"
    return False, ""


# â”€â”€ Main game loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def play_game(p1: Engine, p2: Engine, max_time_ms: int, min_depth: int,
              conf_thresh: float, show_board: bool, quiet: bool,
              game_num: int, max_moves: int = 200):

    START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    moves: list[str] = []
    current_fen = START_FEN
    players = [p1, p2]  # index 0 = white, 1 = black
    color_names = ["White", "Black"]

    p1.new_game()
    p2.new_game()

    print()
    print(bold(cyan(f"{'='*70}")))
    print(bold(cyan(f"  Game {game_num}  |  {p1.label} (W) vs {p2.label} (B)")))
    print(bold(cyan(f"{'='*70}")))

    for ply in range(max_moves):
        side_idx = ply % 2     # 0=white, 1=black
        engine   = players[side_idx]
        color    = color_names[side_idx]
        move_num = ply // 2 + 1

        over, reason = _is_game_over(START_FEN, moves)
        if over:
            print(bold(yellow(f"\n  â•â• {reason} â•â•")))
            return reason

        print()
        print(bold(f"  Move {move_num} ({color}) â€” {engine.label}"))
        print(dim("  " + "â”€"*60))

        # â”€â”€ search_start: this is where eval bar snaps to 0 on dashboard â”€â”€
        print(yellow("  [search_start] searchLive = null  â† eval bar â†’ 0 here"))

        t_start = time.perf_counter()
        bestmove, ponder, history = engine.search(
            START_FEN, moves, max_time_ms, min_depth, conf_thresh, quiet
        )
        elapsed = (time.perf_counter() - t_start) * 1000

        # â”€â”€ search_end â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not history:
            final_eval = red("NO INFO RECEIVED â€” eval_cp=null for entire search!")
            final_pv   = "â€“"
            print(red("  [search_end] WARNING: infoHistory is empty!"))
        else:
            last = history[-1]
            final_eval = last.eval_str
            final_pv   = " ".join(last.pv[:6]) if last.pv else "â€“"
            final_conf = _compute_confidence(history)
            print(green(
                f"  [search_end]  move={bold(bestmove)}  eval={final_eval}  "
                f"depth={last.depth}/{last.seldepth}  "
                f"conf={final_conf:.2f}  time={elapsed:.0f}ms"
            ))
            print(dim(f"  PV: {final_pv}"))

        if not bestmove or bestmove == "(none)":
            print(red("  Engine returned no move â€” game over?"))
            return "No move"

        moves.append(bestmove)
        current_fen = _apply_moves(START_FEN, moves)

        if show_board:
            print()
            print(_render_board(current_fen, bestmove))

    return "Max moves reached"


# â”€â”€ Entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--movetime",   type=int,   default=2000,  help="Hard time ceiling per move (ms)")
    p.add_argument("--games",      type=int,   default=1,     help="Number of games")
    p.add_argument("--board",      action="store_true",       help="Show board after each move")
    p.add_argument("--quiet",      action="store_true",       help="Only show move decisions")
    p.add_argument("--p2-sf",      action="store_true",       help="Player 2 = Stockfish")
    p.add_argument("--min-depth",  type=int,   default=8,     help="Min depth before confidence stop")
    p.add_argument("--conf-thresh",type=float, default=0.75,  help="Confidence threshold to stop")
    p.add_argument("--max-moves",  type=int,   default=200,   help="Auto-adjudicate after N plies")
    args = p.parse_args()

    # Find engines
    bot_path = _find_bot_engine()

    _SF_CANDIDATES = [
        "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe",
        "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64.exe",
        "engines/stockfish/stockfish.exe",
    ]
    def _find_sf():
        for rel in _SF_CANDIDATES:
            p_ = _ROOT / rel
            if p_.exists(): return str(p_)
        raise FileNotFoundError("Stockfish not found")

    print(bold(f"\n  Bot engine : {bot_path}"))

    p1 = Engine(bot_path, threads=1, hash_mb=64, label="Redux")
    if args.p2_sf:
        sf_path = _find_sf()
        print(bold(f"  Opponent   : {sf_path}"))
        p2 = Engine(sf_path, threads=1, hash_mb=64, label="Stockfish")
    else:
        p2 = Engine(bot_path, threads=1, hash_mb=64, label="Redux(B)")

    print(bold(f"  Movetime   : {args.movetime}ms  |  min-depth: {args.min_depth}  |  conf: {args.conf_thresh}"))

    try:
        for g in range(1, args.games + 1):
            result = play_game(
                p1, p2,
                max_time_ms  = args.movetime,
                min_depth    = args.min_depth,
                conf_thresh  = args.conf_thresh,
                show_board   = args.board,
                quiet        = args.quiet,
                game_num     = g,
                max_moves    = args.max_moves,
            )
            print(bold(yellow(f"\n  Result: {result}")))
    finally:
        p1.close()
        p2.close()


if __name__ == "__main__":
    main()
