"""
inspect_bin_position.py â€” Compare one exact NNUE binary position signature across years/files.

The .bin files do not store FENs, only NNUE feature rows plus labels. This tool:
  1. Builds the exact feature signature for a target position from a FEN or move list.
  2. Scans selected .bin files for exact matches on:
       - stm
       - bucket
       - n_white / n_black
       - white feature indices
       - black feature indices
  3. Prints score/WDL statistics by file and by year.

Important:
  - Matching is on the NNUE feature signature, not full FEN metadata.
  - Castling rights / ep squares are NOT encoded in the binary features, so two FENs
    with the same piece placement and side-to-move will match the same signature.

Examples:
  python tools/inspect_bin_position.py --moves e2e4 e7e5 g1f3 b8c6
  python tools/inspect_bin_position.py --moves e2e4 e7e5 g1f3 b8c6 f1c4 g8f6
  python tools/inspect_bin_position.py --fen "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"
  python tools/inspect_bin_position.py --moves e2e4 e7e5 g1f3 b8c6 --year-min 2023
  python tools/inspect_bin_position.py --moves e2e4 e7e5 g1f3 b8c6 --top-values 20
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chess
from ml.arch import piece_count_bucket
from ml.data import RECORD_DTYPE, HEADER_SIZE, MAX_FEATS, _read_header
from ml.features import board_features


@dataclass
class TargetSignature:
    fen: str
    stm: int
    bucket: int
    piece_count: int
    n_white: int
    n_black: int
    white_feats: np.ndarray
    black_feats: np.ndarray


@dataclass
class FileMatchStats:
    path: str
    year: int
    month: int
    matches: int
    scores: np.ndarray
    wdls: np.ndarray


def _board_from_args(args: argparse.Namespace) -> chess.Board:
    if args.fen:
        return chess.Board(args.fen)

    board = chess.Board()
    if args.moves:
        for uci in args.moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError(f"Illegal move for current board: {uci}")
            board.push(move)
    return board


def _signature_from_board(board: chess.Board) -> TargetSignature:
    wf, bf, pc = board_features(board)
    if pc < 3:
        raise ValueError("Target position has too few pieces for NNUE training records")

    n_white = min(len(wf), MAX_FEATS)
    n_black = min(len(bf), MAX_FEATS)
    white = np.zeros(MAX_FEATS, dtype=np.uint16)
    black = np.zeros(MAX_FEATS, dtype=np.uint16)
    white[:n_white] = np.asarray(wf[:n_white], dtype=np.uint16)
    black[:n_black] = np.asarray(bf[:n_black], dtype=np.uint16)

    return TargetSignature(
        fen=board.fen(),
        stm=1 if board.turn == chess.BLACK else 0,
        bucket=piece_count_bucket(pc),
        piece_count=pc,
        n_white=n_white,
        n_black=n_black,
        white_feats=white,
        black_feats=black,
    )


def _iter_bins(year_min: int | None, year_max: int | None) -> list[str]:
    paths = sorted(glob.glob(str(ROOT / 'data' / 'training' / '*.bin')))
    out: list[str] = []
    for path in paths:
        if os.path.getsize(path) <= 32:
            continue
        m = re.search(r'q(\d{4})_(\d{2})\.bin$', os.path.basename(path))
        if not m:
            continue
        year = int(m.group(1))
        if year_min is not None and year < year_min:
            continue
        if year_max is not None and year > year_max:
            continue
        out.append(path)
    return out


def _match_file(path: str, sig: TargetSignature) -> FileMatchStats | None:
    meta = _read_header(path)
    n = meta['n_records']
    arr = np.memmap(path, dtype=RECORD_DTYPE, mode='r', offset=HEADER_SIZE, shape=(n,))

    mask = (
        (arr['stm'] == sig.stm)
        & (arr['bucket'] == sig.bucket)
        & (arr['n_white'] == sig.n_white)
        & (arr['n_black'] == sig.n_black)
    )
    if not np.any(mask):
        return None

    subset = arr[mask]
    white_ok = np.all(subset['white_feats'] == sig.white_feats, axis=1)
    if not np.any(white_ok):
        return None
    subset = subset[white_ok]

    black_ok = np.all(subset['black_feats'] == sig.black_feats, axis=1)
    if not np.any(black_ok):
        return None
    subset = subset[black_ok]

    m = re.search(r'q(\d{4})_(\d{2})\.bin$', os.path.basename(path))
    year = int(m.group(1)) if m else -1
    month = int(m.group(2)) if m else -1
    return FileMatchStats(
        path=path,
        year=year,
        month=month,
        matches=len(subset),
        scores=subset['score'].astype(np.float32).copy(),
        wdls=subset['wdl'].astype(np.float32).copy(),
    )


def _fmt_stats(values: np.ndarray) -> str:
    if len(values) == 0:
        return 'n=0'
    return (
        f"n={len(values):>5,}  mean={values.mean():>8.2f}  std={values.std():>8.2f}"
        f"  min={values.min():>7.1f}  max={values.max():>7.1f}"
    )


def _print_summary(sig: TargetSignature, matches: list[FileMatchStats], top_values: int) -> None:
    print('Target position')
    print(f"  FEN         : {sig.fen}")
    print(f"  STM         : {'black' if sig.stm else 'white'}")
    print(f"  Piece count : {sig.piece_count}")
    print(f"  Bucket      : {sig.bucket}")
    print(f"  n_white     : {sig.n_white}")
    print(f"  n_black     : {sig.n_black}")
    print()

    if not matches:
        print('No exact feature-signature matches found in selected binaries.')
        return

    total = sum(m.matches for m in matches)
    print(f"Matched files: {len(matches)}  total matching records: {total:,}")
    print()

    print('Per file')
    for fm in matches:
        name = os.path.basename(fm.path)
        print(f"  {name}")
        print(f"    score: {_fmt_stats(fm.scores)}")
        print(f"    wdl  : {_fmt_stats(fm.wdls)}")
        if top_values > 0:
            scores = ', '.join(str(int(v)) for v in fm.scores[:top_values])
            wdls = ', '.join(f'{float(v):.3f}' for v in fm.wdls[:top_values])
            print(f"    score values: [{scores}]")
            print(f"    wdl values  : [{wdls}]")
    print()

    by_year: dict[int, list[FileMatchStats]] = defaultdict(list)
    for fm in matches:
        by_year[fm.year].append(fm)

    print('Per year aggregate')
    for year in sorted(by_year):
        scores = np.concatenate([fm.scores for fm in by_year[year]])
        wdls = np.concatenate([fm.wdls for fm in by_year[year]])
        print(f"  {year}")
        print(f"    score: {_fmt_stats(scores)}")
        print(f"    wdl  : {_fmt_stats(wdls)}")

    all_scores = np.concatenate([fm.scores for fm in matches])
    all_wdls = np.concatenate([fm.wdls for fm in matches])
    print()
    print('Global aggregate')
    print(f"  score: {_fmt_stats(all_scores)}")
    print(f"  wdl  : {_fmt_stats(all_wdls)}")


def main() -> None:
    ap = argparse.ArgumentParser(description='Inspect one exact binary position signature across years/files')
    group = ap.add_mutually_exclusive_group(required=False)
    group.add_argument('--fen', help='Target FEN to search for')
    group.add_argument('--moves', nargs='*', help='UCI move sequence from startpos')
    ap.add_argument('--year-min', type=int, default=None, help='Only inspect files from this year onward')
    ap.add_argument('--year-max', type=int, default=None, help='Only inspect files up to this year')
    ap.add_argument('--limit-files', type=int, default=0, help='Only inspect the first N matching files (0 = all)')
    ap.add_argument('--top-values', type=int, default=8, help='How many raw score/WDL values to print per file')
    args = ap.parse_args()

    if not args.fen and not args.moves:
        raise SystemExit('Provide --fen or --moves')

    board = _board_from_args(args)
    sig = _signature_from_board(board)
    paths = _iter_bins(args.year_min, args.year_max)
    if args.limit_files > 0:
        paths = paths[:args.limit_files]

    matches: list[FileMatchStats] = []
    for i, path in enumerate(paths, 1):
        hit = _match_file(path, sig)
        if hit is not None:
            matches.append(hit)
        if i % 25 == 0 or i == len(paths):
            print(f"scanned {i}/{len(paths)} files", file=sys.stderr)

    _print_summary(sig, matches, args.top_values)


if __name__ == '__main__':
    main()
