"""
label_data.py — Extract positions from a Lichess PGN and label with Stockfish.

Takes a (potentially huge) .pgn or .pgn.zst file, samples positions from
high-rated games, and evaluates each with Stockfish at a configurable depth.
Outputs a CSV of (fen, score_cp) pairs ready for NNUE training.

Requirements:
    pip install python-chess zstandard tqdm

Usage:
    # Basic — label 5M positions from a Lichess monthly dump:
    python -m training.label_data --pgn lichess_db_standard_rated_2025-01.pgn.zst --output data.csv --count 5000000

    # Faster with more SF threads:
    python -m training.label_data --pgn games.pgn --output data.csv --count 1000000 --sf-threads 4 --sf-depth 10

    # Use multiple SF workers for throughput:
    python -m training.label_data --pgn games.pgn --output data.csv --workers 4 --sf-depth 10
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import pathlib
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Optional

import chess
import chess.pgn

# Try optional zstd support for compressed Lichess dumps
try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_SF = _ROOT / "engines" / "stockfish-17.1" / "stockfish" / "stockfish-windows-x86-64-avx2.exe"


# ---------------------------------------------------------------------------
# Position filtering
# ---------------------------------------------------------------------------

def should_sample_position(board: chess.Board, ply: int) -> bool:
    """
    Decide whether to include this position as a training sample.
    Filters to keep only interesting, non-trivial positions.
    """
    # Skip first 8 half-moves (opening book territory)
    if ply < 8:
        return False

    # Skip positions with very few pieces (endgame tablebases cover these)
    piece_count = len(board.piece_map())
    if piece_count <= 4:
        return False

    # Skip positions in check (search instability)
    if board.is_check():
        return False

    # Skip positions where game is effectively over
    if board.is_game_over(claim_draw=True):
        return False

    # Quiet filter: skip positions where captures are available.
    # SF evals at depth 14 can be 200-500cp off from deeper search in
    # tactical positions. Quiet positions have much tighter, more reliable
    # labels which reduce the noise floor the network has to fit.
    if any(board.is_capture(m) for m in board.legal_moves):
        return False

    return True


def elo_filter(headers: dict, min_elo: int = 1800) -> bool:
    """Only keep games where both players are above min_elo."""
    try:
        white_elo = int(headers.get("WhiteElo", "0"))
        black_elo = int(headers.get("BlackElo", "0"))
        return white_elo >= min_elo and black_elo >= min_elo
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Position extraction from PGN
# ---------------------------------------------------------------------------

def extract_positions_from_pgn(pgn_path: str, max_positions: int,
                                min_elo: int = 1800,
                                sample_rate: float = 0.125,
                                chunk_size: int = 100000) -> list[tuple]:
    """
    Read a PGN file (optionally .zst compressed) and extract FEN positions.
    Returns a list of (fen, game_result_wdl) tuples.
    game_result_wdl is 1.0 (white win), 0.5 (draw), or 0.0 (black win).
    """
    positions = []
    games_read = 0
    games_used = 0

    print(f"Extracting positions from {pgn_path} ...")
    print(f"  Target: {max_positions:,} positions")
    print(f"  Min Elo: {min_elo}")
    print(f"  Sample rate: {sample_rate}")

    # Open file (handle .zst compression)
    if pgn_path.endswith('.zst'):
        if not HAS_ZSTD:
            print("ERROR: zstandard package needed for .zst files")
            print("  pip install zstandard")
            sys.exit(1)
        raw = open(pgn_path, 'rb')
        dctx = zstd.ZstdDecompressor()
        reader = dctx.stream_reader(raw)
        text_stream = io.TextIOWrapper(reader, encoding='utf-8', errors='replace')
    else:
        text_stream = open(pgn_path, 'r', encoding='utf-8', errors='replace')
        raw = None

    try:
        while len(positions) < max_positions:
            game = chess.pgn.read_game(text_stream)
            if game is None:
                break

            games_read += 1

            # Print progress periodically
            if games_read % 10000 == 0:
                print(f"  Games scanned: {games_read:,}  |  Used: {games_used:,}  |  "
                      f"Positions: {len(positions):,} / {max_positions:,}")

            # Filter by Elo
            headers = dict(game.headers)
            if not elo_filter(headers, min_elo):
                continue

            # Filter out bullet games (too noisy)
            time_control = headers.get("TimeControl", "")
            if time_control:
                try:
                    base = int(time_control.split("+")[0])
                    if base < 60:   # ultra-bullet / bullet < 1 min base
                        continue
                except (ValueError, IndexError):
                    pass

            games_used += 1

            # Derive game result WDL for label blending
            result_str = headers.get("Result", "*")
            result_wdl = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}.get(result_str, 0.5)

            # Walk through the game
            board = game.board()
            ply = 0
            for move in game.mainline_moves():
                board.push(move)
                ply += 1

                if should_sample_position(board, ply):
                    if random.random() < sample_rate:
                        positions.append((board.fen(), result_wdl))
                        if len(positions) >= max_positions:
                            break

    finally:
        text_stream.close()
        if raw:
            raw.close()

    print(f"  Done. Scanned {games_read:,} games, used {games_used:,}, "
          f"extracted {len(positions):,} positions.")
    return positions


# ---------------------------------------------------------------------------
# Stockfish labeling — single worker
# ---------------------------------------------------------------------------

def _label_batch_worker(args: tuple) -> list[tuple[str, int]]:
    """
    Worker function for labeling a batch of FENs with Stockfish.
    Runs in a separate process (via ProcessPoolExecutor).
    Returns list of (fen, score_cp_from_white) tuples.
    """
    fens_with_result, sf_path, sf_depth, sf_hash, sf_threads = args

    import subprocess, time

    proc = subprocess.Popen(
        [str(sf_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', bufsize=1,
    )

    def send(cmd: str):
        proc.stdin.write(cmd + '\n')
        proc.stdin.flush()

    def read_until(keyword: str, timeout: float = 30.0) -> list[str]:
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            lines.append(line)
            if keyword in line:
                return lines
        return lines

    # Init
    send('uci')
    read_until('uciok')
    send(f'setoption name Hash value {sf_hash}')
    send(f'setoption name Threads value {sf_threads}')
    send('isready')
    read_until('readyok')

    results = []
    for fen, game_result_wdl in fens_with_result:
        send(f'position fen {fen}')
        send(f'go depth {sf_depth}')
        output_lines = read_until('bestmove')

        score_cp = None
        is_mate = False
        stm_is_black = fen.split()[1] == 'b'

        for line in output_lines:
            if 'score cp' in line and ' depth ' in line:
                try:
                    # Get score from the deepest info line
                    parts = line.split('score cp')
                    val_str = parts[1].strip().split()[0]
                    score_cp = int(val_str)
                    is_mate = False
                except (IndexError, ValueError):
                    pass
            elif 'score mate' in line and ' depth ' in line:
                try:
                    parts = line.split('score mate')
                    mate_in = int(parts[1].strip().split()[0])
                    score_cp = 30000 if mate_in > 0 else -30000
                    is_mate = True
                except (IndexError, ValueError):
                    pass

        if score_cp is None:
            continue   # Skip failed evaluations

        # Convert to white's perspective
        score_white = -score_cp if stm_is_black else score_cp

        # Skip extreme non-mate scores (probably tablebase wins/losses)
        if not is_mate and abs(score_white) > 10000:
            continue

        # Clamp mate scores
        score_white = max(-30000, min(30000, score_white))

        # Blend SF eval WDL (70%) with actual game result WDL (30%).
        # The game result is ground truth: it penalises the network for
        # believing SF's "winning" eval in games that were actually lost.
        import math as _math
        sf_wdl = 1.0 / (1.0 + _math.exp(-score_white / 600.0))
        blended_wdl = 0.7 * sf_wdl + 0.3 * game_result_wdl

        results.append((fen, score_white, blended_wdl))

    # Cleanup
    try:
        send('quit')
        proc.wait(timeout=3)
    except:
        proc.kill()

    return results


def label_positions(positions: list[tuple], sf_path: str,
                    sf_depth: int = 10, sf_hash: int = 64,
                    sf_threads: int = 1, workers: int = 1,
                    batch_size: int = 500) -> list[tuple[str, int]]:
    """
    Label a list of FEN positions with Stockfish evaluations.
    Uses multiple workers for throughput.
    """
    print(f"\nLabeling {len(positions):,} positions with Stockfish depth {sf_depth} ...")
    print(f"  SF path: {sf_path}")
    print(f"  Workers: {workers}, Threads/worker: {sf_threads}, Hash/worker: {sf_hash} MB")

    # Split positions into batches
    batches = []
    for i in range(0, len(positions), batch_size):
        batch = positions[i:i + batch_size]
        batches.append((batch, sf_path, sf_depth, sf_hash, sf_threads))

    results = []
    n_done = 0
    t0 = time.time()

    if workers <= 1:
        # Single-process mode (simpler, good for debugging)
        for batch_args in batches:
            batch_results = _label_batch_worker(batch_args)
            results.extend(batch_results)
            n_done += len(batch_args[0])
            elapsed = time.time() - t0
            rate = n_done / elapsed if elapsed > 0 else 0
            eta = (len(positions) - n_done) / rate if rate > 0 else 0
            print(f"  Labeled: {n_done:,}/{len(positions):,}  "
                  f"({len(results):,} kept)  "
                  f"Rate: {rate:.0f} pos/s  ETA: {eta/60:.1f} min")
    else:
        # Multi-process mode
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_label_batch_worker, b): len(b[0]) for b in batches}

            for future in as_completed(futures):
                batch_results = future.result()
                results.extend(batch_results)
                n_done += futures[future]
                elapsed = time.time() - t0
                rate = n_done / elapsed if elapsed > 0 else 0
                eta = (len(positions) - n_done) / rate if rate > 0 else 0
                print(f"  Labeled: {n_done:,}/{len(positions):,}  "
                      f"({len(results):,} kept)  "
                      f"Rate: {rate:.0f} pos/s  ETA: {eta/60:.1f} min")

    elapsed = time.time() - t0
    print(f"  Done. Labeled {len(results):,} positions in {elapsed/60:.1f} minutes "
          f"({len(results)/elapsed:.0f} pos/s)")
    return results


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(results: list[tuple], output_path: str):
    """Write labeled positions to CSV with game-result-blended WDL column."""
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['fen', 'score', 'wdl'])
        for fen, score, wdl in results:
            writer.writerow([fen, score, f"{wdl:.6f}"])
    print(f"\nWrote {len(results):,} samples to {output_path}")

    # Print score distribution
    scores = [s for _, s, _ in results]
    if scores:
        import statistics
        print(f"  Score distribution:")
        print(f"    Mean:   {statistics.mean(scores):.0f} cp")
        if len(scores) >= 2:
            print(f"    Stdev:  {statistics.stdev(scores):.0f} cp")
        print(f"    Min:    {min(scores)} cp")
        print(f"    Max:    {max(scores)} cp")
        print(f"    Median: {statistics.median(scores):.0f} cp")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract positions from Lichess PGN and label with Stockfish"
    )
    parser.add_argument("--pgn", required=True,
                        help="Path to PGN file (supports .pgn and .pgn.zst)")
    parser.add_argument("--output", default="training_data.csv",
                        help="Output CSV path (default: training_data.csv)")
    parser.add_argument("--count", type=int, default=5_000_000,
                        help="Target number of positions to extract (default: 5M)")
    parser.add_argument("--min-elo", type=int, default=1800,
                        help="Minimum Elo for both players (default: 1800)")
    parser.add_argument("--sf-path", type=str, default=str(_DEFAULT_SF),
                        help="Path to Stockfish executable")
    parser.add_argument("--sf-depth", type=int, default=14,
                        help="Stockfish search depth for labeling (default: 14)")
    parser.add_argument("--sf-threads", type=int, default=1,
                        help="Threads per Stockfish worker (default: 1)")
    parser.add_argument("--sf-hash", type=int, default=64,
                        help="Hash table MB per SF worker (default: 64)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of parallel SF workers (default: 1)")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Positions per worker batch (default: 500)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)

    # Validate SF path
    if not os.path.isfile(args.sf_path):
        print(f"ERROR: Stockfish not found at {args.sf_path}")
        sys.exit(1)

    # Step 1: Extract positions
    positions = extract_positions_from_pgn(
        args.pgn,
        max_positions=args.count,
        min_elo=args.min_elo,
    )

    if not positions:
        print("ERROR: No positions extracted. Check the PGN file and filters.")
        sys.exit(1)

    # Shuffle to avoid temporal correlation
    random.shuffle(positions)

    # Step 2: Label with Stockfish
    results = label_positions(
        positions,
        sf_path=args.sf_path,
        sf_depth=args.sf_depth,
        sf_hash=args.sf_hash,
        sf_threads=args.sf_threads,
        workers=args.workers,
        batch_size=args.batch_size,
    )

    # Step 3: Write output
    write_csv(results, args.output)

    print(f"\n{'='*60}")
    print(f"Next steps:")
    print(f"  1. Train:  python -m training.train_nnue --data {args.output} --epochs 100")
    print(f"  2. Export: python -m training.export_weights --checkpoint best_model.pt --output nn.bin")
    print(f"  3. Test:   build\\lichess-bot.exe  (auto-loads nn.bin)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
