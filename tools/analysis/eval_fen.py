#!/usr/bin/env python3
"""
eval_fen.py â€” Evaluate one or more FEN positions with the LichessBotRedux engine.

Shows depth-by-depth PV + score, then prints the bestmove and final eval.

Usage
-----
  # Single FEN:
    python tools/eval_fen.py "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"

  # Multiple FENs:
    python tools/eval_fen.py "FEN1" "FEN2"

  # From a text file (one FEN per line, # = comment):
    python tools/eval_fen.py --file my_positions.txt

  # With extra options:
    python tools/eval_fen.py --movetime 3000 --depth 20 --threads 4 "FEN"

  # Use specific engine binary:
    python tools/eval_fen.py --engine path/to/engine "FEN"

Notes
-----
  - FEN may be "startpos" or a full 6-field FEN or a 4-field FEN (halfmove/fullmove optional).
  - Scores are always from the perspective of the side to move (cp = centipawns).
  - Pass --moves "e2e4 e7e5" after the FEN to test a position reached via moves:
      python tools/eval_fen.py --moves "e2e4 e7e5" startpos
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


sys.path.insert(0, str(_ROOT / "tools"))
from engine_config import find_engine, find_nnue


def run_engine(engine_path: str, fen: str, movetime: int,
               depth: int | None, threads: int, moves: str | None) -> None:
    """Spawn the engine, feed UCI commands, and print results."""

    env = os.environ.copy()
    mingw = r"C:\mingw64\bin"
    if os.path.isdir(mingw) and mingw not in env.get("PATH", ""):
        env["PATH"] = mingw + os.pathsep + env.get("PATH", "")

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    # Build position command
    if fen.strip().lower() == "startpos":
        pos_cmd = "position startpos"
    else:
        # Accept 4-field FENs by padding halfmove/fullmove if missing
        parts = fen.strip().split()
        if len(parts) == 4:
            fen = fen.strip() + " 0 1"
        pos_cmd = f"position fen {fen}"
    if moves:
        pos_cmd += f" moves {moves.strip()}"

    # Build go command
    if depth is not None:
        go_cmd = f"go depth {depth}"
    else:
        go_cmd = f"go movetime {movetime}"

    cmds = [
        "uci\n",
        f"setoption name Threads value {threads}\n",
        "setoption name Hash value 128\n",
        "isready\n",
        f"{pos_cmd}\n",
        f"{go_cmd}\n",
    ]

    proc = subprocess.Popen(
        engine_path,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env, creationflags=flags,
    )

    # Send UCI commands
    for cmd in cmds:
        proc.stdin.write(cmd)
        proc.stdin.flush()

    lines: list[str] = []

    def reader():
        for ln in proc.stdout:
            ln = ln.rstrip()
            lines.append(ln)
            if ln.startswith("bestmove"):
                proc.stdin.write("quit\n")
                proc.stdin.flush()
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # Timeout: movetime + generous buffer (or depth * 10s cap)
    timeout = (depth * 10) if depth else (movetime / 1000 + 5)
    t.join(timeout + 3)
    try:
        proc.kill()
    except OSError:
        pass

    # â”€â”€ Print results â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(f"\n{'â”€'*68}")
    print(f"  FEN: {fen}")
    if moves:
        print(f"  Moves: {moves}")
    print(f"{'â”€'*68}")

    best_score = None
    best_move  = None
    ponder     = None

    for ln in lines:
        if ln.startswith("info") and "depth" in ln and "score" in ln:
            parts = ln.split()
            depth_val = score_str = pv_str = None
            i = 0
            while i < len(parts):
                if parts[i] == "depth":
                    depth_val = parts[i + 1]
                elif parts[i] == "score":
                    kind = parts[i + 1]  # cp | mate | lowerbound | upperbound
                    val  = parts[i + 2] if (i + 2) < len(parts) else "?"
                    if kind in ("cp", "mate"):
                        score_str = f"{kind} {val}"
                        best_score = score_str
                elif parts[i] == "pv":
                    pv_str = " ".join(parts[i + 1 : i + 9])
                elif parts[i] in ("lowerbound", "upperbound"):
                    # score cp N lowerbound/upperbound â€” skip extra token
                    pass
                i += 1
            # Only print full depth lines (skip seldepth / currmove lines)
            if depth_val and score_str:
                pv_part = f"  pv={pv_str}" if pv_str else ""
                print(f"  d={depth_val:>3}  score={score_str:<16}{pv_part}")

        elif ln.startswith("bestmove"):
            toks = ln.split()
            best_move = toks[1] if len(toks) > 1 else "?"
            ponder    = toks[3] if len(toks) > 3 and toks[2] == "ponder" else None

    print(f"{'â”€'*68}")
    ponder_str = f"  ponder={ponder}" if ponder else ""
    print(f"  bestmove={best_move}  score={best_score}{ponder_str}")
    print()


def load_fens_from_file(path: str) -> list[str]:
    result = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            result.append(line)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate FEN position(s) with the LichessBotRedux engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "fens", nargs="*",
        help='FEN string(s) to evaluate. Use "startpos" for the start position.',
    )
    parser.add_argument(
        "--file", "-f", default=None,
        help="Text file with one FEN per line (# = comment).",
    )
    parser.add_argument(
        "--movetime", "-t", type=int, default=2000,
        help="Search time per position in ms (default: 2000). Ignored if --depth set.",
    )
    parser.add_argument(
        "--depth", "-d", type=int, default=None,
        help="Search to a fixed depth instead of movetime.",
    )
    parser.add_argument(
        "--threads", type=int, default=1,
        help="Engine threads (default: 1).",
    )
    parser.add_argument(
        "--moves", "-m", default=None,
        help='Move sequence to play before evaluating (e.g. "e2e4 e7e5").',
    )
    parser.add_argument(
        "--engine", "-e", default=None,
        help="Path to engine binary.",
    )
    args = parser.parse_args()

    fens: list[str] = list(args.fens)
    if args.file:
        fens += load_fens_from_file(args.file)

    if not fens:
        parser.print_help()
        return 1

    try:
        engine = args.engine or find_engine()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Engine: {engine}")

    for fen in fens:
        run_engine(engine, fen, args.movetime, args.depth, args.threads, args.moves)

    return 0


if __name__ == "__main__":
    sys.exit(main())
