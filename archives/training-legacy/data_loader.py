"""
data_loader.py — Load and prepare training data for the NNUE trainer.

Expected CSV format:
  fen,score
  rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1,0
  ...

score is in centipawns from WHITE's perspective.

Each sample is converted to:
  - A sparse feature set (list of active input indices) for both perspectives
  - The score from the side-to-move's perspective
"""

import csv
import numpy as np
import chess
from typing import List, Tuple
from torch.utils.data import Dataset


# ============================================================================
# Feature encoding — must match src/nnue/nnue_arch.h exactly
# ============================================================================

INPUT_SIZE = 768
HIDDEN_SIZE = 256

PIECE_TYPE_MAP = {
    chess.PAWN:   0,  # piece_type_1based - 1
    chess.KNIGHT: 1,
    chess.BISHOP: 2,
    chess.ROOK:   3,
    chess.QUEEN:  4,
    chess.KING:   5,
}


def feature_index(color: int, piece_type_idx: int, square: int) -> int:
    """
    color: 0=white, 1=black
    piece_type_idx: 0=pawn .. 5=king (already 0-based)
    square: 0..63 (a1=0, h8=63)
    """
    return color * 384 + piece_type_idx * 64 + square


def board_to_features(board: chess.Board) -> Tuple[List[int], List[int]]:
    """
    Extract active feature indices for both perspectives.

    Returns:
        (white_features, black_features) — each a list of active input indices
    """
    white_features = []
    black_features = []

    for sq in range(64):
        piece = board.piece_at(sq)
        if piece is None:
            continue

        color = 0 if piece.color == chess.WHITE else 1
        pt_idx = PIECE_TYPE_MAP[piece.piece_type]

        # White perspective: literal
        white_features.append(feature_index(color, pt_idx, sq))

        # Black perspective: flip color, mirror square vertically
        black_features.append(feature_index(color ^ 1, pt_idx, sq ^ 56))

    return white_features, black_features


# ============================================================================
# Dataset
# ============================================================================

class NNUEDataset(Dataset):
    """
    Loads training data from a CSV file.

    Each item returns:
      white_features: np.array of active input indices (white perspective)
      black_features: np.array of active input indices (black perspective)
      stm: 0 for white, 1 for black
      score: float, centipawns from STM's perspective

    Args:
      csv_path:     Path to CSV (columns: fen, score_cp)
      max_samples:  Hard cap on samples loaded (0 = unlimited)
      score_cap:    Discard positions whose |score| > score_cap (0 = keep all).
                    Use ~3000 to strip mate/clamped outliers.
    """

    def __init__(self, csv_path: str, max_samples: int = 0, score_cap: int = 0):
        self.samples: List[Tuple[str, int]] = []
        n_read = 0
        n_filtered = 0

        with open(csv_path, 'r') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            # Accept headerless files too
            if header and not header[0].startswith('r') and header[0] != 'fen':
                # Not a FEN and not a header — treat as data
                try:
                    score = int(header[1])
                    n_read += 1
                    if score_cap == 0 or abs(score) <= score_cap:
                        self.samples.append((header[0], score))
                    else:
                        n_filtered += 1
                except (ValueError, IndexError):
                    pass

            for row in reader:
                if len(row) < 2:
                    continue
                fen = row[0].strip()
                try:
                    score = int(row[1].strip())
                except ValueError:
                    try:
                        score = int(float(row[1].strip()))
                    except ValueError:
                        continue
                n_read += 1
                if score_cap > 0 and abs(score) > score_cap:
                    n_filtered += 1
                    continue
                self.samples.append((fen, score))
                if max_samples > 0 and len(self.samples) >= max_samples:
                    break

        if score_cap > 0 and n_filtered > 0:
            pct = 100.0 * n_filtered / max(n_read, 1)
            print(f"  Score cap |score| <= {score_cap} cp: filtered {n_filtered:,} / {n_read:,} "
                  f"({pct:.1f}%) extreme positions")
        print(f"Loaded {len(self.samples):,} training samples from {csv_path}")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        fen, score_white = self.samples[idx]
        board = chess.Board(fen)

        white_features, black_features = board_to_features(board)

        stm = 0 if board.turn == chess.WHITE else 1

        # Convert score to STM perspective
        score_stm = score_white if stm == 0 else -score_white

        return {
            'white_features': np.array(white_features, dtype=np.int64),
            'black_features': np.array(black_features, dtype=np.int64),
            'stm': stm,
            'score': float(score_stm),
        }


def collate_fn(batch):
    """Custom collate to handle variable-length feature lists."""
    import torch

    batch_size = len(batch)

    # Build sparse input tensors (batch_size × INPUT_SIZE)
    white_input = torch.zeros(batch_size, INPUT_SIZE, dtype=torch.float32)
    black_input = torch.zeros(batch_size, INPUT_SIZE, dtype=torch.float32)
    stm = torch.zeros(batch_size, dtype=torch.long)
    scores = torch.zeros(batch_size, dtype=torch.float32)

    for i, sample in enumerate(batch):
        for idx in sample['white_features']:
            white_input[i, idx] = 1.0
        for idx in sample['black_features']:
            black_input[i, idx] = 1.0
        stm[i] = sample['stm']
        scores[i] = sample['score']

    return white_input, black_input, stm, scores
