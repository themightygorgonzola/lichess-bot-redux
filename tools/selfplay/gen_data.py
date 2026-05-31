"""
gen_data.py â€” Generate high-quality NNUE training data via Stockfish self-play.

Produces positions evaluated by Stockfish with game-result blending for WDL,
filtered for quiet positions only. Output is CSV (fen, score_cp, wdl) ready
for conversion to .bin via prep_data.py.

Self-play protocol:
  1. From startpos, play `random_plies` random legal moves (opening diversity)
  2. Let Stockfish play both sides at `play_depth` until game ends or max_plies
  3. At each position after the opening:
     - Skip if in check
     - Skip if last move was a capture or promotion
     - Evaluate with Stockfish at `eval_depth` (deeper than play_depth)
     - Record: FEN, eval in cp (white POV), game result
  4. After game ends, backfill WDL = lambda*sigmoid(cp/600) + (1-lambda)*result
     where result âˆˆ {1.0, 0.5, 0.0} (white win/draw/loss)

Usage:
  python tools/gen_data.py --output data/training/sf_selfplay_1m.csv --games 30000
  python tools/gen_data.py --output data/training/sf_selfplay_1m.csv --games 30000 --workers 4

Then convert to binary:
  python tools/prep_data.py --csv data/training/sf_selfplay_1m.csv --output data/training/sf_selfplay_1m.bin
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import chess

# â”€â”€ Defaults â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DEFAULT_SF_PATH = str(Path(__file__).resolve().parent.parent.parent
                      / "engines" / "stockfish-17.1" / "stockfish"
                      / "stockfish-windows-x86-64-avx2.exe")
DEFAULT_EVAL_DEPTH = 10
DEFAULT_PLAY_DEPTH = 8
DEFAULT_RANDOM_PLIES = 8
DEFAULT_MAX_PLIES = 300
DEFAULT_SCORE_CAP = 3000
DEFAULT_WDL_LAMBDA = 0.75  # blend: 75% engine eval, 25% game result
WDL_SCALE = 600.0


def _cp_to_wp(cp: float) -> float:
    """Convert centipawns (white POV) to win probability [0,1]."""
    return 1.0 / (1.0 + math.exp(-cp / WDL_SCALE))


class StockfishUCI:
    """Minimal UCI wrapper for Stockfish."""

    def __init__(self, path: str, hash_mb: int = 16, threads: int = 1):
        self.proc = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._send("uci")
        self._wait_for("uciok")
        self._send(f"setoption name Hash value {hash_mb}")
        self._send(f"setoption name Threads value {threads}")
        self._send("isready")
        self._wait_for("readyok")

    def _send(self, cmd: str):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _wait_for(self, token: str) -> list[str]:
        lines = []
        while True:
            line = self.proc.stdout.readline().strip()
            lines.append(line)
            if line.startswith(token):
                return lines

    def evaluate(self, board: chess.Board, depth: int) -> tuple[int, str]:
        """Return (score_cp_white_pov, bestmove_uci)."""
        self._send(f"position fen {board.fen()}")
        self._send(f"go depth {depth}")
        score_cp = 0
        bestmove = ""
        while True:
            line = self.proc.stdout.readline().strip()
            if line.startswith("info") and " score " in line:
                parts = line.split()
                try:
                    idx = parts.index("score")
                    if parts[idx + 1] == "cp":
                        score_cp = int(parts[idx + 2])
                    elif parts[idx + 1] == "mate":
                        mate_in = int(parts[idx + 2])
                        score_cp = 30000 if mate_in > 0 else -30000
                except (IndexError, ValueError):
                    pass
            if line.startswith("bestmove"):
                bestmove = line.split()[1] if len(line.split()) > 1 else ""
                break

        # Convert to white POV
        if board.turn == chess.BLACK:
            score_cp = -score_cp

        return score_cp, bestmove

    def close(self):
        try:
            self._send("quit")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _play_one_game(sf: StockfishUCI, play_depth: int, eval_depth: int,
                   random_plies: int, max_plies: int, score_cap: int,
                   wdl_lambda: float) -> list[tuple[str, int, float]]:
    """
    Play one self-play game, return list of (fen, score_cp_white, wdl_white).
    """
    board = chess.Board()
    positions: list[tuple[str, int]] = []  # (fen, score_cp_white) for quiet positions

    # Phase 1: random opening
    for _ in range(random_plies):
        legal = list(board.legal_moves)
        if not legal:
            break
        board.push(random.choice(legal))

    if board.is_game_over():
        return []

    # Phase 2: SF self-play
    ply = 0
    last_was_capture = True  # treat opening moves as "not quiet"

    while not board.is_game_over() and ply < max_plies:
        ply += 1

        # Evaluate position for data (only if quiet)
        is_quiet = (not board.is_check() and not last_was_capture and ply > 2)

        if is_quiet:
            score_cp, _ = sf.evaluate(board, eval_depth)
            score_cp = max(-score_cap, min(score_cap, score_cp))
            positions.append((board.fen(), score_cp))

        # Play move at play_depth
        _, bestmove_uci = sf.evaluate(board, play_depth)
        if not bestmove_uci or bestmove_uci == "(none)":
            break

        try:
            move = chess.Move.from_uci(bestmove_uci)
            last_was_capture = board.is_capture(move)
            board.push(move)
        except (ValueError, chess.IllegalMoveError):
            break

    # Determine game result (white POV)
    result = board.result()
    if result == "1-0":
        game_result = 1.0
    elif result == "0-1":
        game_result = 0.0
    else:
        game_result = 0.5

    # Backfill WDL with game-result blending
    output = []
    for fen, score_cp in positions:
        wp_eval = _cp_to_wp(score_cp)
        wdl = wdl_lambda * wp_eval + (1 - wdl_lambda) * game_result
        output.append((fen, score_cp, wdl))

    return output


def _worker(args: tuple) -> list[tuple[str, int, float]]:
    """Worker process: plays N games and returns all positions."""
    sf_path, n_games, play_depth, eval_depth, random_plies, max_plies, score_cap, wdl_lambda, seed = args

    random.seed(seed)
    sf = StockfishUCI(sf_path, hash_mb=16, threads=1)
    all_positions = []

    for i in range(n_games):
        try:
            positions = _play_one_game(sf, play_depth, eval_depth,
                                       random_plies, max_plies, score_cap, wdl_lambda)
            all_positions.extend(positions)
        except Exception as e:
            print(f"  Worker game {i} error: {e}", file=sys.stderr)

    sf.close()
    return all_positions


def main():
    parser = argparse.ArgumentParser(description="Generate NNUE training data via Stockfish self-play")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--sf-path", default=DEFAULT_SF_PATH, help="Stockfish binary path")
    parser.add_argument("--games", type=int, default=10000, help="Number of self-play games")
    parser.add_argument("--workers", type=int, default=1, help="Parallel Stockfish instances")
    parser.add_argument("--eval-depth", type=int, default=DEFAULT_EVAL_DEPTH, help="Depth for position evaluation")
    parser.add_argument("--play-depth", type=int, default=DEFAULT_PLAY_DEPTH, help="Depth for move selection")
    parser.add_argument("--random-plies", type=int, default=DEFAULT_RANDOM_PLIES, help="Random opening plies")
    parser.add_argument("--max-plies", type=int, default=DEFAULT_MAX_PLIES, help="Max plies per game")
    parser.add_argument("--score-cap", type=int, default=DEFAULT_SCORE_CAP, help="Score cap in centipawns")
    parser.add_argument("--wdl-lambda", type=float, default=DEFAULT_WDL_LAMBDA, help="WDL blend: lambda*eval + (1-lambda)*result")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    if not os.path.isfile(args.sf_path):
        print(f"Error: Stockfish not found at {args.sf_path}")
        sys.exit(1)

    print(f"Stockfish: {args.sf_path}")
    print(f"Games: {args.games}  Workers: {args.workers}")
    print(f"Eval depth: {args.eval_depth}  Play depth: {args.play_depth}")
    print(f"Random plies: {args.random_plies}  Max plies: {args.max_plies}")
    print(f"Score cap: {args.score_cap}  WDL lambda: {args.wdl_lambda}")

    t0 = time.time()

    # Split games across workers
    games_per_worker = args.games // max(args.workers, 1)
    remainder = args.games % max(args.workers, 1)

    worker_args = []
    for w in range(args.workers):
        n = games_per_worker + (1 if w < remainder else 0)
        if n > 0:
            worker_args.append((
                args.sf_path, n, args.play_depth, args.eval_depth,
                args.random_plies, args.max_plies, args.score_cap,
                args.wdl_lambda, args.seed + w
            ))

    all_positions = []

    if args.workers <= 1:
        # Single-process mode
        all_positions = _worker(worker_args[0])
        print(f"  Collected {len(all_positions):,} positions from {args.games} games")
    else:
        # Multi-process mode
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_worker, wa): i for i, wa in enumerate(worker_args)}
            for future in as_completed(futures):
                positions = future.result()
                all_positions.extend(positions)
                print(f"  Worker done: +{len(positions):,} positions (total: {len(all_positions):,})")

    elapsed = time.time() - t0
    print(f"\nGenerated {len(all_positions):,} positions in {elapsed:.0f}s "
          f"({len(all_positions)/max(elapsed,1):.0f} pos/s)")

    # Write CSV
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['fen', 'score_cp', 'wdl'])
        for fen, score_cp, wdl in all_positions:
            writer.writerow([fen, score_cp, f"{wdl:.6f}"])

    print(f"Saved to {args.output}")
    print(f"\nConvert to binary format with:")
    print(f"  python tools/prep_data.py --csv {args.output} --output {args.output.replace('.csv', '.bin')}")


if __name__ == "__main__":
    main()
