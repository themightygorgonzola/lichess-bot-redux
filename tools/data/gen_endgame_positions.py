"""
gen_endgame_positions.py â€” Generate endgame training positions annotated by deep Stockfish.

Produces NNUE binary records (RECORD_DTYPE) for piece-count buckets 0-2 by:
  1. Sampling random legal positions for a chosen material distribution
     (KQ-K, KR-K, KP-K, KBN-K, KQ-KR, KR-KB, KRPP-KRP, ...)
  2. Running Stockfish at fixed depth (default 22) to obtain the eval
  3. Filtering for quiet positions (not in check, no winning capture)
  4. Encoding as RECORD_DTYPE bytes

Output is a standard .bin file compatible with mean-alltime-dedup-shuffled.bin â€”
ready to be appended/merged.

Usage:
    python tools/gen_endgame_positions.py --output data/processed/endgames.bin \
        --target 4_000_000 --workers 4 --depth 22
"""

from __future__ import annotations

import argparse
import math
import os
import random
import struct
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import chess
import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from ml.data import RECORD_DTYPE, RECORD_SIZE, HEADER_SIZE, HEADER_MAGIC, MAX_FEATS
from ml.features import board_features
from ml.arch import piece_count_bucket, INPUT_SIZE

DEFAULT_SF_PATH = str(ROOT / "engines" / "stockfish-17.1" / "stockfish"
                      / "stockfish-windows-x86-64-avx2.exe")

WDL_SCALE = 600.0
SCORE_CAP = 2000   # match the rest of the dataset


# â”€â”€ Material distribution â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Each entry: (white_pieces, black_pieces, weight)
# Pieces specified as a string of piece chars (excluding king, which is implicit).
# Weight controls relative sample frequency.
MATERIAL_TABLE = [
    # KvK trivial draws â€” small frequency, helps anchor the "0 = draw" signal
    ("",     "",      1),

    # Decisive single-pawn endings
    ("P",    "",      4),
    ("",     "p",     4),
    ("PP",   "",      4),
    ("",     "pp",    4),
    ("PPP",  "",      3),
    ("",     "ppp",   3),

    # Pawn vs pawn
    ("P",    "p",     6),
    ("PP",   "p",     5),
    ("P",    "pp",    5),
    ("PP",   "pp",    5),
    ("PPP",  "pp",    4),
    ("PP",   "ppp",   4),

    # Minor piece endings
    ("N",    "",      3),    # KNvK = draw
    ("",     "n",     3),
    ("B",    "",      3),
    ("",     "b",     3),
    ("BN",   "",      4),    # KBN-K = mate
    ("",     "bn",    4),
    ("BB",   "",      4),    # KBB-K = mate
    ("",     "bb",    4),

    # Minor + pawn
    ("NP",   "",      4),
    ("",     "np",    4),
    ("BP",   "",      4),
    ("",     "bp",    4),
    ("N",    "p",     4),
    ("B",    "p",     4),
    ("NP",   "p",     4),
    ("BP",   "p",     4),

    # Major piece endings
    ("R",    "",      4),    # KR-K mate
    ("",     "r",     4),
    ("Q",    "",      3),    # KQ-K mate
    ("",     "q",     3),
    ("R",    "p",     5),    # KR vs KP
    ("",     "rp",    0),    # mirror handled implicitly by random side selection
    ("R",    "r",     6),    # rook ending (often drawn)
    ("Q",    "q",     5),    # queen ending
    ("R",    "n",     4),
    ("R",    "b",     4),
    ("Q",    "r",     5),
    ("Q",    "b",     4),
    ("Q",    "n",     4),

    # Rook + pawn endings (the canonical "must learn" endgame class)
    ("RP",   "r",     8),
    ("RPP",  "r",     7),
    ("RPP",  "rp",    8),
    ("RPPP", "rp",    6),
    ("RPP",  "rpp",   8),
    ("RPPP", "rpp",   6),

    # Queen + pawn endings
    ("QP",   "q",     5),
    ("QPP",  "q",     4),
    ("QPP",  "qp",    5),

    # Major vs minor + pawn
    ("R",    "bp",    4),
    ("R",    "np",    4),
    ("Q",    "rp",    4),

    # Q vs RR (drawn fortress class)
    ("Q",    "rr",    3),
]


# â”€â”€ Random position generator â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _piece_char_to_python(c: str):
    return chess.Piece.from_symbol(c)


def _random_position(white_extra: str, black_extra: str, rng: random.Random,
                     max_attempts: int = 30) -> chess.Board | None:
    """Place kings + extra pieces randomly on legal squares.

    Returns a Board or None if we couldn't make a legal one.
    """
    for _ in range(max_attempts):
        board = chess.Board.empty()
        # 1. Place kings â€” must not be adjacent
        squares = list(chess.SQUARES)
        rng.shuffle(squares)
        wk = squares.pop()
        # find a valid black king square
        bk = None
        for sq in squares:
            if chess.square_distance(wk, sq) >= 2:
                bk = sq
                break
        if bk is None:
            continue
        board.set_piece_at(wk, chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(bk, chess.Piece(chess.KING, chess.BLACK))

        # 2. Place extras
        used = {wk, bk}

        ok = True
        for piece_chars, color in ((white_extra, chess.WHITE),
                                   (black_extra, chess.BLACK)):
            for c in piece_chars:
                placed = False
                # try several squares
                for sq in rng.sample(range(64), 64):
                    if sq in used:
                        continue
                    # pawns can't be on first/last rank
                    if c.lower() == 'p':
                        rank = chess.square_rank(sq)
                        if rank == 0 or rank == 7:
                            continue
                    used.add(sq)
                    pt = {'p': chess.PAWN, 'n': chess.KNIGHT,
                          'b': chess.BISHOP, 'r': chess.ROOK,
                          'q': chess.QUEEN}[c.lower()]
                    board.set_piece_at(sq, chess.Piece(pt, color))
                    placed = True
                    break
                if not placed:
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue

        # 3. Random side to move
        board.turn = chess.WHITE if rng.random() < 0.5 else chess.BLACK

        # 4. Validate position
        if not board.is_valid():
            continue
        # Skip already-game-over positions (no eval signal)
        if board.is_game_over(claim_draw=False):
            continue
        return board
    return None


def _flatten_material_table():
    flat = []
    for w, b, weight in MATERIAL_TABLE:
        if weight <= 0:
            continue
        for _ in range(weight):
            flat.append((w, b))
    return flat


# â”€â”€ Stockfish UCI wrapper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class _SF:
    def __init__(self, path: str, hash_mb: int = 64, threads: int = 1):
        self.proc = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
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

    def _wait_for(self, token: str):
        while True:
            line = self.proc.stdout.readline().strip()
            if line.startswith(token):
                return line
            if not line and self.proc.poll() is not None:
                raise RuntimeError("Stockfish exited unexpectedly")

    def evaluate(self, fen: str, depth: int) -> int | None:
        """Return score in cp from white's perspective, or None on failure."""
        self._send(f"position fen {fen}")
        self._send(f"go depth {depth}")
        last_score_cp = None
        last_score_mate = None
        while True:
            line = self.proc.stdout.readline().strip()
            if not line:
                if self.proc.poll() is not None:
                    return None
                continue
            if line.startswith("info") and " score " in line:
                # Parse "score cp N" or "score mate M"
                parts = line.split()
                try:
                    si = parts.index("score")
                    kind = parts[si + 1]
                    val = int(parts[si + 2])
                    if kind == "cp":
                        last_score_cp = val
                        last_score_mate = None
                    elif kind == "mate":
                        last_score_cp = None
                        last_score_mate = val
                except (ValueError, IndexError):
                    pass
            elif line.startswith("bestmove"):
                break

        # Convert mate to cp using a large value (will be capped)
        if last_score_mate is not None:
            cp_white = (3000 if last_score_mate > 0 else -3000)
        elif last_score_cp is not None:
            cp_white = last_score_cp
        else:
            return None

        # Sign by side-to-move: SF reports score from the STM perspective.
        # Convert to white's perspective.
        board = chess.Board(fen)
        if board.turn == chess.BLACK:
            cp_white = -cp_white
        return cp_white

    def close(self):
        try:
            self._send("quit")
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


# â”€â”€ Worker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _worker(args):
    sf_path, depth, n_positions, seed, score_cap = args
    rng = random.Random(seed)
    flat = _flatten_material_table()

    sf = _SF(sf_path, hash_mb=32, threads=1)
    out = np.zeros(n_positions, dtype=RECORD_DTYPE)
    good = 0
    skipped_check = 0
    skipped_capture = 0
    skipped_setup = 0
    skipped_eval = 0

    try:
        target = n_positions
        attempts = 0
        max_total_attempts = n_positions * 50
        while good < target and attempts < max_total_attempts:
            attempts += 1
            white_extra, black_extra = rng.choice(flat)
            board = _random_position(white_extra, black_extra, rng)
            if board is None:
                skipped_setup += 1
                continue
            if board.is_check():
                skipped_check += 1
                continue
            # quiet filter: skip if any capture is available
            if any(board.is_capture(mv) for mv in board.legal_moves):
                skipped_capture += 1
                continue

            fen = board.fen()
            score_cp = sf.evaluate(fen, depth)
            if score_cp is None:
                skipped_eval += 1
                continue

            # Cap and derive WDL
            score_cp = max(-score_cap, min(score_cap, score_cp))
            wdl = 1.0 / (1.0 + math.exp(-score_cp / WDL_SCALE))

            wf, bf, pc = board_features(board)
            if pc < 3:
                continue
            n_w = min(len(wf), MAX_FEATS)
            n_b = min(len(bf), MAX_FEATS)

            r = out[good]
            r['score']      = np.int16(int(score_cp))
            r['wdl']        = np.float16(float(wdl))
            r['stm']        = 1 if board.turn == chess.BLACK else 0
            r['bucket']     = piece_count_bucket(pc)
            r['n_white']    = n_w
            r['n_black']    = n_b
            r['white_feats'][:n_w] = wf[:n_w]
            r['black_feats'][:n_b] = bf[:n_b]
            good += 1
    finally:
        sf.close()

    return (out[:good].tobytes(),
            {'good': good, 'attempts': attempts,
             'check': skipped_check, 'capture': skipped_capture,
             'setup': skipped_setup, 'eval': skipped_eval})


# â”€â”€ Driver â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--target", type=int, default=4_000_000,
                   help="Total positions to generate (default 4M)")
    p.add_argument("--workers", type=int, default=max(1, os.cpu_count() // 2))
    p.add_argument("--depth", type=int, default=22,
                   help="Stockfish search depth per position (default 22)")
    p.add_argument("--sf-path", default=DEFAULT_SF_PATH)
    p.add_argument("--score-cap", type=int, default=SCORE_CAP)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--shard-size", type=int, default=20_000,
                   help="Positions per worker batch (default 20000)")
    args = p.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not Path(args.sf_path).exists():
        print(f"ERROR: Stockfish not found at {args.sf_path}")
        sys.exit(2)

    print(f"Output:    {out_path}")
    print(f"Target:    {args.target:,} positions")
    print(f"Workers:   {args.workers}")
    print(f"Depth:     {args.depth}")
    print(f"Score cap: Â±{args.score_cap} cp")
    print(f"Sharding:  {args.shard_size:,} positions per task")
    print()

    n_shards = (args.target + args.shard_size - 1) // args.shard_size
    print(f"Total shards: {n_shards}")

    rng = random.Random(args.seed)
    shard_args = []
    for i in range(n_shards):
        target_this_shard = min(args.shard_size, args.target - i * args.shard_size)
        shard_args.append((args.sf_path, args.depth, target_this_shard,
                           rng.randint(0, 2**31 - 1), args.score_cap))

    # Open output file with header placeholder
    with open(out_path, 'wb') as f:
        f.write(b'\x00' * HEADER_SIZE)

    total_records = 0
    total_attempts = 0
    skipped = {'check': 0, 'capture': 0, 'setup': 0, 'eval': 0}
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_worker, a) for a in shard_args]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                blob, stats = fut.result()
            except Exception as e:
                print(f"  shard failed: {e}")
                continue
            with open(out_path, 'ab') as f:
                f.write(blob)
            total_records += stats['good']
            total_attempts += stats['attempts']
            for k in skipped:
                skipped[k] += stats[k]
            elapsed = time.time() - t0
            rate = total_records / max(elapsed, 1)
            eta = (args.target - total_records) / max(rate, 1e-6)
            print(f"  shard {i}/{n_shards} done â€” total {total_records:,} kept "
                  f"({rate:.0f} pos/s, ETA {int(eta/60)}m)")

    # Write final header
    with open(out_path, 'r+b') as f:
        f.seek(0)
        f.write(struct.pack('<8sIII12x', HEADER_MAGIC, 1, total_records, INPUT_SIZE))

    elapsed = time.time() - t0
    print()
    print(f"Done. {total_records:,} records written in {elapsed/60:.1f}m")
    print(f"  Output size: {(HEADER_SIZE + total_records * RECORD_SIZE)/1024**3:.2f} GB")
    print(f"  Acceptance:  {total_records / max(total_attempts,1) * 100:.1f}% "
          f"({total_attempts:,} attempts)")
    print(f"  Skipped:     check={skipped['check']:,}  capture={skipped['capture']:,}  "
          f"setup={skipped['setup']:,}  eval={skipped['eval']:,}")


if __name__ == "__main__":
    main()
