#!/usr/bin/env python3
"""
compare_engines.py â€” Side-by-side engine comparison for a specific position.

Runs both H-035 and Stockfish 17.1 on the same FEN at multiple depths/times,
printing a live depth-by-depth comparison showing where their evaluations
diverge and whose PV is better.

Usage
-----
  # Critical position from game mNnTB2Ca (move 63):
    python tools/compare_engines.py "R7/2B5/PPk5/6p1/5pK1/1r6/8/8 b - - 5 63"

  # With moves leading up to a position:
    python tools/compare_engines.py startpos --moves "e2e4 e7e5 g1f3"

  # With explicit movetime:
    python tools/compare_engines.py "FEN" --movetime 3000

  # With depth limit:
    python tools/compare_engines.py "FEN" --depth 30

  # Compare only specific moves (score each with both engines):
    python tools/compare_engines.py "FEN" --compare f4f3 b3g3

  # Suppress board rendering:
    python tools/compare_engines.py "FEN" --no-board
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# Force UTF-8 on Windows
if sys.platform == "win32":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import chess
import chess.pgn

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from uci.engine import UCIEngine, SearchResult

# ---------------------------------------------------------------------------
# ANSI
# ---------------------------------------------------------------------------
try:
    import ctypes as _c
    _c.windll.kernel32.SetConsoleMode(_c.windll.kernel32.GetStdHandle(-11), 7)
except Exception:
    pass

_NO_COLOR = False

def _esc(code: str, t: str) -> str:
    return t if _NO_COLOR else f"\033[{code}m{t}\033[0m"

def green(t):   return _esc("92", t)
def red(t):     return _esc("91", t)
def yellow(t):  return _esc("93", t)
def cyan(t):    return _esc("96", t)
def bold(t):    return _esc("1",  t)
def dim(t):     return _esc("2",  t)
def magenta(t): return _esc("95", t)
def blue(t):    return _esc("94", t)

# ---------------------------------------------------------------------------
# Engine discovery
# ---------------------------------------------------------------------------
sys.path.insert(0, str(_ROOT / "tools"))
from engine_config import find_engine as _find_bot_engine

_SF_CANDIDATES = [
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe",
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64.exe",
    "engines/stockfish/stockfish.exe",
]

def find_engine(candidates: list[str], hint: Optional[str] = None) -> str:
    """Find engine binary. For the bot engine, delegates to engine_config."""
    if hint:
        if os.path.isfile(hint):
            return os.path.abspath(hint)
        raise FileNotFoundError(f"Engine not found: {hint!r}")
    # Try engine_config first (bot engine)
    try:
        return _find_bot_engine()
    except FileNotFoundError:
        pass
    # Fallback: try SF candidates
    for rel in candidates:
        p = _ROOT / rel
        if p.exists():
            return str(p)
    raise FileNotFoundError(f"No engine found among: {candidates}")

# ---------------------------------------------------------------------------
# Board rendering
# ---------------------------------------------------------------------------
def render_board(board: chess.Board, last_move: Optional[chess.Move] = None,
                 flip: bool = False) -> str:
    if _NO_COLOR:
        return str(board)
    LIGHT = "\033[48;5;223m"; DARK = "\033[48;5;130m"; HL = "\033[48;5;184m"
    RST = "\033[0m"; WP = "\033[97m"; BP = "\033[30m"
    UNI = {"P":"â™™","N":"â™˜","B":"â™—","R":"â™–","Q":"â™•","K":"â™”",
           "p":"â™Ÿ","n":"â™ž","b":"â™","r":"â™œ","q":"â™›","k":"â™š"}
    hl = set()
    if last_move:
        hl.add(last_move.from_square); hl.add(last_move.to_square)
    ranks = range(7,-1,-1) if not flip else range(0,8)
    files = range(0,8)  if not flip else range(7,-1,-1)
    lines = []
    for r in ranks:
        row = f" {r+1} "
        for f in files:
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
    fl = "   " + "  ".join(" abcdefgh"[f+1] for f in files)
    lines.append(fl)
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Multi-PV live reader â€” streams all depths from a running engine process
# ---------------------------------------------------------------------------
@dataclass
class PVLine:
    rank:     int
    depth:    int
    seldepth: int = 0
    score_cp:   Optional[int] = None
    score_mate: Optional[int] = None
    nodes:    int = 0
    nps:      int = 0
    time_ms:  int = 0
    pv:       list[str] = field(default_factory=list)

    @property
    def score_str(self) -> str:
        if self.score_mate is not None:
            m = self.score_mate
            if m > 0: return green(f"+M{m}")
            if m < 0: return red(f"-M{abs(m)}")
            return "M0"
        if self.score_cp is not None:
            cp = self.score_cp
            col = (green if cp >= 50 else (red if cp <= -50 else yellow))
            sign = "+" if cp > 0 else ""
            return col(f"{sign}{cp/100:.2f}")
        return "?"

    @property
    def score_raw(self) -> int:
        """Normalized score for sorting/comparison (higher = better for side to move)."""
        if self.score_mate is not None:
            return (10000 - abs(self.score_mate)) * (1 if self.score_mate > 0 else -1)
        return self.score_cp or 0


def parse_info_line(line: str) -> Optional[PVLine]:
    parts = line.split()
    if not parts or parts[0] != "info":
        return None
    pv = PVLine(rank=1, depth=0)
    i = 1
    while i < len(parts):
        t = parts[i]
        if t == "depth" and i+1 < len(parts):
            pv.depth = int(parts[i+1]); i += 2
        elif t == "seldepth" and i+1 < len(parts):
            pv.seldepth = int(parts[i+1]); i += 2
        elif t == "multipv" and i+1 < len(parts):
            pv.rank = int(parts[i+1]); i += 2
        elif t == "score" and i+2 < len(parts):
            if parts[i+1] == "cp":
                pv.score_cp = int(parts[i+2]); pv.score_mate = None; i += 3
            elif parts[i+1] == "mate":
                pv.score_mate = int(parts[i+2]); pv.score_cp = None; i += 3
            else:
                i += 1
        elif t == "nodes" and i+1 < len(parts):
            pv.nodes = int(parts[i+1]); i += 2
        elif t == "nps" and i+1 < len(parts):
            pv.nps = int(parts[i+1]); i += 2
        elif t == "time" and i+1 < len(parts):
            pv.time_ms = int(parts[i+1]); i += 2
        elif t == "pv":
            pv.pv = parts[i+1:]; break
        else:
            i += 1
    return pv if pv.depth > 0 else None


def pv_to_san(board: chess.Board, pv_uci: list[str], max_moves: int = 6) -> str:
    tmp = board.copy(); parts = []
    for tok in pv_uci[:max_moves]:
        try:
            m = chess.Move.from_uci(tok)
            parts.append(tmp.san(m)); tmp.push(m)
        except Exception:
            break
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Single-engine deep analysis (blocking, returns list of PVLines per depth)
# ---------------------------------------------------------------------------
def analyse(engine_path: str, fen: str, moves: Optional[str],
            movetime_ms: Optional[int], depth: Optional[int],
            threads: int, hash_mb: int, multipv: int,
            label: str, color_fn) -> tuple[list[PVLine], float]:
    """
    Run analysis. Returns (final_pvlines, elapsed_s).
    final_pvlines is the set of multipv lines at the deepest completed depth.
    """
    env = os.environ.copy()
    mingw = r"C:\mingw64\bin"
    if os.path.isdir(mingw) and mingw not in env.get("PATH",""):
        env["PATH"] = mingw + os.pathsep + env.get("PATH","")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    pos_cmd = f"position fen {fen}"
    if moves:
        pos_cmd += f" moves {moves.strip()}"

    go_cmd = "go"
    if movetime_ms is not None:
        go_cmd += f" movetime {movetime_ms}"
    elif depth is not None:
        go_cmd += f" depth {depth}"
    else:
        go_cmd += " movetime 1000"

    commands = [
        "uci\n",
        f"setoption name Threads value {threads}\n",
        f"setoption name Hash value {hash_mb}\n",
        f"setoption name MultiPV value {multipv}\n",
        "isready\n",
        f"{pos_cmd}\n",
        f"{go_cmd}\n",
    ]

    proc = subprocess.Popen(
        [engine_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=env, creationflags=flags,
    )
    for cmd in commands:
        proc.stdin.write(cmd)
    proc.stdin.flush()

    # Collect output
    pvs_by_depth: dict[int, dict[int, PVLine]] = {}  # depth -> rank -> PVLine
    bestmove = None
    t0 = time.monotonic()

    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.rstrip()
        if line.startswith("info ") and "score" in line:
            pv = parse_info_line(line)
            if pv:
                if pv.depth not in pvs_by_depth:
                    pvs_by_depth[pv.depth] = {}
                pvs_by_depth[pv.depth][pv.rank] = pv
        elif line.startswith("bestmove"):
            bestmove = line.split()[1] if len(line.split()) > 1 else None
            break

    elapsed = time.monotonic() - t0
    proc.stdin.write("quit\n")
    proc.stdin.flush()
    proc.wait(timeout=3)

    # Return the deepest completed depth that has all multipv ranks
    if not pvs_by_depth:
        return [], elapsed

    # Find deepest depth that has at least rank 1
    best_depth = max(
        (d for d, ranks in pvs_by_depth.items() if 1 in ranks),
        default=0
    )
    final = list(pvs_by_depth[best_depth].values())
    final.sort(key=lambda x: x.rank)

    # Mark the bestmove (engine may report different from rank-1 when time was cut)
    if bestmove and final:
        for pv in final:
            if pv.pv and pv.pv[0] == bestmove:
                break

    return final, elapsed


# ---------------------------------------------------------------------------
# Score a specific move with both engines (for --compare mode)
# ---------------------------------------------------------------------------
def score_move(engine_path: str, fen: str, move_uci: str,
               movetime_ms: int, threads: int, hash_mb: int) -> PVLine:
    """Get engine's evaluation of the position AFTER playing move_uci."""
    env = os.environ.copy()
    mingw = r"C:\mingw64\bin"
    if os.path.isdir(mingw) and mingw not in env.get("PATH",""):
        env["PATH"] = mingw + os.pathsep + env.get("PATH","")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    commands = [
        "uci\n",
        f"setoption name Threads value {threads}\n",
        f"setoption name Hash value {hash_mb}\n",
        "isready\n",
        f"position fen {fen} moves {move_uci}\n",
        f"go movetime {movetime_ms}\n",
    ]
    proc = subprocess.Popen(
        [engine_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=env, creationflags=flags,
    )
    for cmd in commands:
        proc.stdin.write(cmd)
    proc.stdin.flush()

    result = PVLine(rank=1, depth=0)
    while True:
        line = proc.stdout.readline()
        if not line: break
        line = line.rstrip()
        if line.startswith("info ") and "score" in line:
            pv = parse_info_line(line)
            if pv:
                # Keep highest depth
                if pv.depth > result.depth:
                    result = pv
        elif line.startswith("bestmove"):
            break

    proc.stdin.write("quit\n"); proc.stdin.flush()
    proc.wait(timeout=3)

    # Negate: score was from opponent's perspective after our move
    if result.score_cp is not None:
        result.score_cp = -result.score_cp
    if result.score_mate is not None:
        result.score_mate = -result.score_mate
    return result


# ---------------------------------------------------------------------------
# Main comparison display
# ---------------------------------------------------------------------------
def compare(
    fen: str,
    moves: Optional[str],
    bot_path: str,
    sf_path: str,
    movetime_ms: Optional[int],
    depth: Optional[int],
    threads: int,
    hash_mb: int,
    multipv: int,
    compare_moves: Optional[list[str]],
    no_board: bool,
):
    board = chess.Board(fen)
    if moves:
        for mv in moves.strip().split():
            board.push(chess.Move.from_uci(mv))

    side = "White" if board.turn == chess.WHITE else "Black"
    flip = (board.turn == chess.BLACK)

    print()
    print(bold("=" * 72))
    print(bold("  ENGINE COMPARISON"))
    print(bold("=" * 72))
    print(f"  FEN    : {cyan(fen)}")
    if moves:
        print(f"  Moves  : {dim(moves)}")
    print(f"  Side   : {bold(side)}")
    if movetime_ms:
        print(f"  Mode   : movetime {movetime_ms}ms  |  MultiPV {multipv}")
    elif depth:
        print(f"  Mode   : depth {depth}  |  MultiPV {multipv}")
    print(f"  Bot    : {blue(os.path.basename(bot_path))}")
    print(f"  SF     : {magenta(os.path.basename(sf_path))}")
    print()

    if not no_board:
        print(render_board(board, flip=flip))
        print()

    # â”€â”€ Run both engines in parallel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    bot_result: list[PVLine] = []
    sf_result:  list[PVLine] = []
    bot_elapsed = 0.0
    sf_elapsed  = 0.0

    bot_err: Optional[Exception] = None
    sf_err:  Optional[Exception] = None

    def run_bot():
        nonlocal bot_result, bot_elapsed, bot_err
        try:
            bot_result, bot_elapsed = analyse(
                bot_path, fen, moves, movetime_ms, depth, threads, hash_mb, multipv,
                "Bot", blue)
        except Exception as e:
            bot_err = e

    def run_sf():
        nonlocal sf_result, sf_elapsed, sf_err
        try:
            sf_result, sf_elapsed = analyse(
                sf_path, fen, moves, movetime_ms, depth, threads, hash_mb, multipv,
                "SF", magenta)
        except Exception as e:
            sf_err = e

    t_bot = threading.Thread(target=run_bot)
    t_sf  = threading.Thread(target=run_sf)
    print(dim("  Running both engines in parallel..."))
    t_bot.start(); t_sf.start()
    t_bot.join(); t_sf.join()

    if bot_err:
        print(red(f"  Bot engine error: {bot_err}")); return
    if sf_err:
        print(red(f"  Stockfish error: {sf_err}")); return

    # â”€â”€ Side-by-side table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    bot_top = bot_result[0] if bot_result else None
    sf_top  = sf_result[0]  if sf_result  else None

    print()
    print(bold("=" * 72))
    print(bold("  TOP MOVE COMPARISON"))
    print(bold("=" * 72))

    # Collect all move-san for both to determine agreement
    bot_moves_san = {}
    sf_moves_san  = {}

    if bot_top:
        for pv in bot_result:
            if pv.pv:
                try:
                    san = pv_to_san(board, pv.pv[:1])
                    bot_moves_san[pv.rank] = (pv.pv[0], san)
                except Exception:
                    bot_moves_san[pv.rank] = (pv.pv[0], pv.pv[0])

    if sf_top:
        for pv in sf_result:
            if pv.pv:
                try:
                    san = pv_to_san(board, pv.pv[:1])
                    sf_moves_san[pv.rank] = (pv.pv[0], san)
                except Exception:
                    sf_moves_san[pv.rank] = (pv.pv[0], pv.pv[0])

    # Find bot's top move rank in SF's list
    bot_uci = bot_moves_san.get(1, (None, None))[0] if bot_top else None
    sf_rank_of_bot  = next((rank for rank, (uci, _) in sf_moves_san.items()  if uci == bot_uci), None)
    bot_rank_of_sf1 = next((rank for rank, (uci, _) in bot_moves_san.items() if uci == (sf_moves_san.get(1, (None,None))[0])), None)

    print()
    print(f"  {'Rank':<5}  {blue('Bot'):<40}  {magenta('Stockfish 17.1')}")
    print(f"  {dim('-'*5)}  {dim('-'*36)}  {dim('-'*36)}")
    for rank in range(1, multipv + 1):
        bot_pv = next((p for p in bot_result if p.rank == rank), None)
        sf_pv  = next((p for p in sf_result  if p.rank == rank), None)

        bot_cell = ""
        sf_cell  = ""

        if bot_pv and bot_pv.pv:
            b_san = pv_to_san(board, bot_pv.pv[:1])
            b_pv  = pv_to_san(board, bot_pv.pv, max_moves=4)
            b_score = bot_pv.score_str
            b_depth = f"d{bot_pv.depth}"
            # Highlight if bot's rank-1 is SF's rank-1
            if rank == 1 and sf_rank_of_bot == 1:
                b_san_fmt = green(b_san)
            elif rank == 1 and sf_rank_of_bot and sf_rank_of_bot <= 3:
                b_san_fmt = yellow(b_san)
            elif rank == 1:
                b_san_fmt = red(b_san)
            else:
                b_san_fmt = dim(b_san)
            bot_cell = f"{b_san_fmt} {b_score} {dim(b_depth)}"
        else:
            bot_cell = dim("â€”")

        if sf_pv and sf_pv.pv:
            s_san = pv_to_san(board, sf_pv.pv[:1])
            s_score = sf_pv.score_str
            s_depth = f"d{sf_pv.depth}"
            if rank == 1 and bot_rank_of_sf1 == 1:
                s_san_fmt = green(s_san)
            elif rank == 1 and bot_rank_of_sf1 and bot_rank_of_sf1 <= 3:
                s_san_fmt = yellow(s_san)
            elif rank == 1:
                s_san_fmt = magenta(s_san)
            else:
                s_san_fmt = dim(s_san)
            sf_cell = f"{s_san_fmt} {s_score} {dim(s_depth)}"
        else:
            sf_cell = dim("â€”")

        print(f"  #{rank:<4}  {bot_cell:<50}  {sf_cell}")

    # â”€â”€ Agreement summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print()
    print(bold("=" * 72))
    print(bold("  SUMMARY"))
    print(bold("=" * 72))
    print()

    if not bot_top or not sf_top:
        print(yellow("  Warning: one engine produced no result."))
        return

    bot_best_san = bot_moves_san.get(1, (None,"?"))[1]
    sf_best_san  = sf_moves_san.get(1,  (None,"?"))[1]
    bot_best_uci = bot_moves_san.get(1, (None,None))[0]
    sf_best_uci  = sf_moves_san.get(1,  (None,None))[0]

    if bot_best_uci == sf_best_uci:
        verdict = green("AGREE â€” both play " + str(bot_best_san))
    elif sf_rank_of_bot and sf_rank_of_bot <= 3:
        verdict = yellow(f"CLOSE â€” Bot: {bot_best_san}, SF top move: {sf_best_san} (bot's move is SF rank {sf_rank_of_bot})")
    else:
        verdict = red(f"DISAGREE â€” Bot: {bot_best_san}  |  SF: {sf_best_san}")

    print(f"  {verdict}")

    # Score gap
    if bot_top.score_cp is not None and sf_top.score_cp is not None:
        gap = sf_top.score_cp - bot_top.score_cp
        gap_abs = abs(gap)
        gap_str = f"{gap:+d}cp"
        if gap_abs < 30:
            print(f"  Score gap : {green(gap_str)}  (SF rates position similarly)")
        elif gap_abs < 100:
            print(f"  Score gap : {yellow(gap_str)}  (SF sees a moderate difference)")
        else:
            print(f"  Score gap : {red(gap_str)}  (SF sees a materially different position)")
    elif bot_top.score_mate is not None or sf_top.score_mate is not None:
        print(f"  Bot score : {bot_top.score_str}   SF score : {sf_top.score_str}")

    bot_best_pv = pv_to_san(board, (bot_result[0].pv if bot_result else []), max_moves=8)
    sf_best_pv  = pv_to_san(board, (sf_result[0].pv  if sf_result  else []), max_moves=8)
    print()
    print(f"  {blue('Bot PV')} : {bot_best_pv}")
    print(f"  {magenta('SF  PV')} : {sf_best_pv}")

    print(f"\n  Search time : {blue(f'Bot {bot_elapsed*1000:.0f}ms')}  |  {magenta(f'SF {sf_elapsed*1000:.0f}ms')}")

    depth_bot = bot_result[0].depth if bot_result else 0
    depth_sf  = sf_result[0].depth  if sf_result  else 0
    print(f"  Depth       : {blue(f'Bot d{depth_bot}'):<30}  {magenta(f'SF d{depth_sf}')}")

    nps_bot = bot_result[0].nps if bot_result else 0
    nps_sf  = sf_result[0].nps  if sf_result  else 0
    if nps_bot and nps_sf:
        nps_ratio = nps_sf / nps_bot
        print(f"  NPS         : {blue(f'Bot {nps_bot//1000}kn/s'):<30}  "
              f"{magenta(f'SF {nps_sf//1000}kn/s')}  "
              f"{dim(f'(SF is {nps_ratio:.1f}x faster)')}")

    # â”€â”€ Move comparison mode â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if compare_moves:
        print()
        print(bold("=" * 72))
        print(bold("  MOVE-BY-MOVE COMPARISON"))
        print(bold("=" * 72))
        mt = max(200, (movetime_ms or 1000) // 2)
        print(dim(f"  Scoring each candidate with both engines at movetime {mt}ms..."))
        print()
        print(f"  {'Move':<10}  {blue('Bot eval'):<20}  {magenta('SF eval'):<20}  Verdict")
        print(f"  {dim('-'*8)}  {dim('-'*18)}  {dim('-'*18)}  {dim('-'*20)}")
        for uci in compare_moves:
            try:
                brd_tmp = board.copy()
                mv = chess.Move.from_uci(uci)
                san = brd_tmp.san(mv)
            except Exception:
                san = uci
            bot_s = score_move(bot_path, fen, uci, mt, threads, hash_mb)
            sf_s  = score_move(sf_path,  fen, uci, mt, threads, hash_mb)
            bot_v = bot_s.score_str
            sf_v  = sf_s.score_str
            # Compare
            if bot_s.score_cp is not None and sf_s.score_cp is not None:
                diff = bot_s.score_cp - sf_s.score_cp
                if abs(diff) < 30:
                    verdict = green("agree")
                elif diff > 0:
                    verdict = yellow(f"Bot +{diff}cp optimistic")
                else:
                    verdict = yellow(f"Bot {diff}cp pessimistic")
            elif bot_s.score_mate is not None and sf_s.score_mate is not None:
                verdict = green("both see mate") if bot_s.score_mate == sf_s.score_mate else yellow("mate in N differs")
            else:
                verdict = dim("â€”")
            print(f"  {san:<10}  {bot_v:<30}  {sf_v:<30}  {verdict}")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    global _NO_COLOR
    ap = argparse.ArgumentParser(
        description="Side-by-side comparison of H-035 vs Stockfish 17.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("fen", help="FEN string or 'startpos'")
    ap.add_argument("--moves", default=None, help="UCI move sequence after FEN")
    ap.add_argument("--movetime", type=int, default=None,
                    help="Milliseconds per engine (default: 2000)")
    ap.add_argument("--depth", type=int, default=None,
                    help="Depth limit instead of movetime")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--hash", dest="hash_mb", type=int, default=128)
    ap.add_argument("--multipv", type=int, default=5,
                    help="Number of candidate moves to show (default: 5)")
    ap.add_argument("--compare", nargs="+", metavar="MOVE",
                    help="Score specific UCI moves with both engines")
    ap.add_argument("--bot", default=None, help="Path to bot engine")
    ap.add_argument("--sf",  default=None, help="Path to Stockfish")
    ap.add_argument("--no-board", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    _NO_COLOR = args.no_color

    try:
        bot_path = find_engine(_BOT_CANDIDATES, args.bot)
        sf_path  = find_engine(_SF_CANDIDATES,  args.sf)
    except FileNotFoundError as e:
        print(red(f"Error: {e}"), file=sys.stderr); sys.exit(1)

    movetime = args.movetime
    if movetime is None and args.depth is None:
        movetime = 2000

    compare(
        fen=args.fen,
        moves=args.moves,
        bot_path=bot_path,
        sf_path=sf_path,
        movetime_ms=movetime,
        depth=args.depth,
        threads=args.threads,
        hash_mb=args.hash_mb,
        multipv=args.multipv,
        compare_moves=args.compare,
        no_board=args.no_board,
    )


if __name__ == "__main__":
    main()
