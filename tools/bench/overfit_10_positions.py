"""
overfit_10_positions.py â€” Hard overfit test for the NNUE training pipeline.

Goal:
  Prove the model and optimizer can memorize 10 fixed positions to within 0.1 cp.

Method:
  - Build 10 deterministic positions from fixed move sequences.
  - Query Stockfish once for target evals.
  - Train the current NNUE model on ONLY those 10 positions.
  - Fail if max absolute error never reaches the requested threshold.

Usage:
  python tools/overfit_10_positions.py
  python tools/overfit_10_positions.py --device cuda --max-epochs 20000 --target-mae 0.1
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.engine
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.arch import piece_count_bucket
from ml.data import MAX_FEATS
from ml.features import board_features
from ml.model import NNUE

DEFAULT_ENGINE = ROOT / 'engines' / 'stockfish-17.1' / 'stockfish' / 'stockfish-windows-x86-64-avx2.exe'

POSITION_SPECS = [
    ('Italian', ['e2e4', 'e7e5', 'g1f3', 'b8c6', 'f1c4', 'f8c5', 'c2c3', 'g8f6', 'd2d3', 'd7d6']),
    ('QGD', ['d2d4', 'd7d5', 'c2c4', 'e7e6', 'b1c3', 'g8f6', 'c1g5', 'f8e7', 'e2e3', 'e8g8']),
    ('Ruy Lopez', ['e2e4', 'e7e5', 'g1f3', 'b8c6', 'f1b5', 'a7a6', 'b5a4', 'g8f6', 'e1g1', 'f8e7']),
    ('Najdorf', ['e2e4', 'c7c5', 'g1f3', 'd7d6', 'd2d4', 'c5d4', 'f3d4', 'g8f6', 'b1c3', 'a7a6']),
    ('English', ['c2c4', 'e7e5', 'b1c3', 'g8f6', 'g2g3', 'd7d5', 'c4d5', 'f6d5', 'f1g2', 'd5b6']),
    ('Caro-Kann', ['e2e4', 'c7c6', 'd2d4', 'd7d5', 'b1c3', 'd5e4', 'c3e4', 'c8f5', 'e4g3', 'f5g6']),
    ('French', ['e2e4', 'e7e6', 'd2d4', 'd7d5', 'b1c3', 'g8f6', 'c1g5', 'f8e7', 'e4e5', 'f6d7']),
    ('Slav', ['d2d4', 'd7d5', 'c2c4', 'c7c6', 'g1f3', 'g8f6', 'b1c3', 'd5c4', 'a2a4', 'c8f5']),
    ('King Indian', ['d2d4', 'g8f6', 'c2c4', 'g7g6', 'b1c3', 'f8g7', 'e2e4', 'd7d6', 'g1f3', 'e8g8']),
    ('Queen Indian', ['d2d4', 'g8f6', 'c2c4', 'e7e6', 'g1f3', 'b7b6', 'g2g3', 'c8b7', 'f1g2', 'f8b4']),
]


@dataclass
class PositionRecord:
    name: str
    fen: str
    target_cp: float
    white_idx: np.ndarray
    white_cnt: int
    black_idx: np.ndarray
    black_cnt: int
    stm: int
    bucket: int


def _board_from_moves(moves: list[str]) -> chess.Board:
    board = chess.Board()
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f'Illegal move {uci} for board {board.fen()}')
        board.push(move)
    return board


def _tensorize_board(board: chess.Board) -> tuple[np.ndarray, int, np.ndarray, int, int, int]:
    wf, bf, pc = board_features(board)
    n_white = min(len(wf), MAX_FEATS)
    n_black = min(len(bf), MAX_FEATS)
    white = np.zeros(MAX_FEATS, dtype=np.int32)
    black = np.zeros(MAX_FEATS, dtype=np.int32)
    white[:n_white] = np.asarray(wf[:n_white], dtype=np.int32)
    black[:n_black] = np.asarray(bf[:n_black], dtype=np.int32)
    stm = 1 if board.turn == chess.BLACK else 0
    bucket = piece_count_bucket(pc)
    return white, n_white, black, n_black, stm, bucket


def _target_cp(engine: chess.engine.SimpleEngine, board: chess.Board, depth: int) -> float:
    info = engine.analyse(board, chess.engine.Limit(depth=depth))
    score = info['score'].pov(board.turn).score(mate_score=3000)
    if score is None:
        raise ValueError(f'No score returned for {board.fen()}')
    return float(score)


def _build_dataset(engine_path: Path, depth: int) -> list[PositionRecord]:
    out: list[PositionRecord] = []
    with chess.engine.SimpleEngine.popen_uci(str(engine_path)) as engine:
        for name, moves in POSITION_SPECS:
            board = _board_from_moves(moves)
            target = _target_cp(engine, board, depth)
            white, n_white, black, n_black, stm, bucket = _tensorize_board(board)
            out.append(PositionRecord(
                name=name,
                fen=board.fen(),
                target_cp=target,
                white_idx=white,
                white_cnt=n_white,
                black_idx=black,
                black_cnt=n_black,
                stm=stm,
                bucket=bucket,
            ))
    return out


def _stack(records: list[PositionRecord], device: torch.device) -> dict[str, torch.Tensor]:
    return {
        'white_idx': torch.tensor(np.stack([r.white_idx for r in records]), dtype=torch.int32, device=device),
        'white_cnt': torch.tensor([r.white_cnt for r in records], dtype=torch.int32, device=device),
        'black_idx': torch.tensor(np.stack([r.black_idx for r in records]), dtype=torch.int32, device=device),
        'black_cnt': torch.tensor([r.black_cnt for r in records], dtype=torch.int32, device=device),
        'stm': torch.tensor([r.stm for r in records], dtype=torch.long, device=device),
        'bucket': torch.tensor([r.bucket for r in records], dtype=torch.long, device=device),
        'target': torch.tensor([r.target_cp for r in records], dtype=torch.float32, device=device),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='Overfit 10 positions to sub-0.1cp error')
    ap.add_argument('--engine', default=str(DEFAULT_ENGINE), help='Path to Stockfish executable')
    ap.add_argument('--engine-depth', type=int, default=12, help='Stockfish depth for target labels')
    ap.add_argument('--device', default='auto', help='cuda/cpu/auto')
    ap.add_argument('--lr', type=float, default=0.01, help='Learning rate')
    ap.add_argument('--max-epochs', type=int, default=20000, help='Maximum optimization steps')
    ap.add_argument('--target-mae', type=float, default=0.1, help='Pass threshold for mean absolute error in cp')
    ap.add_argument('--target-maxerr', type=float, default=0.1, help='Pass threshold for worst-case absolute error in cp')
    ap.add_argument('--print-every', type=int, default=100, help='Progress print interval')
    args = ap.parse_args()

    engine_path = Path(args.engine)
    if not engine_path.is_file():
        raise SystemExit(f'Engine not found: {engine_path}')

    if args.device == 'auto':
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device(args.device)

    print(f'Device: {device}')
    print(f'Engine: {engine_path}')
    print(f'Engine depth: {args.engine_depth}')
    print('Building 10-position dataset...')
    records = _build_dataset(engine_path, args.engine_depth)
    batch = _stack(records, device)

    print('\nTargets')
    for r in records:
        side = 'b' if r.stm else 'w'
        print(f"  {r.name:12s}  target={r.target_cp:8.1f}cp  stm={side}  bucket={r.bucket}  fen={r.fen}")

    model = NNUE().to(device)
    model.train()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=0.0)

    t0 = time.time()
    success = False
    final_pred = None

    for epoch in range(1, args.max_epochs + 1):
        optimizer.zero_grad(set_to_none=True)
        pred = model(
            batch['white_idx'], batch['white_cnt'],
            batch['black_idx'], batch['black_cnt'],
            batch['stm'], batch['bucket'],
        ).squeeze(1)
        diff = pred - batch['target']
        loss = torch.mean(diff * diff)
        loss.backward()
        optimizer.step()

        with torch.no_grad():
            abs_err = torch.abs(diff)
            mae = float(abs_err.mean().item())
            max_err = float(abs_err.max().item())
            rmse = float(torch.sqrt(torch.mean(diff * diff)).item())
            final_pred = pred.detach().cpu().numpy()

        if epoch % args.print_every == 0 or epoch == 1:
            print(f"epoch={epoch:>6d}  loss={loss.item():>12.6f}  mae={mae:>9.4f}  max_err={max_err:>9.4f}  rmse={rmse:>9.4f}")

        if mae <= args.target_mae and max_err <= args.target_maxerr:
            success = True
            print(f"\nPASS at epoch {epoch}: mae={mae:.4f}cp  max_err={max_err:.4f}cp")
            break

    elapsed = time.time() - t0
    print(f'\nElapsed: {elapsed:.1f}s')
    print('Final predictions')
    for r, p in zip(records, final_pred.tolist()):
        err = p - r.target_cp
        print(f"  {r.name:12s}  target={r.target_cp:8.2f}  pred={p:8.2f}  err={err:8.3f}")

    if not success:
        print('\nFAIL: did not reach requested error threshold.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
