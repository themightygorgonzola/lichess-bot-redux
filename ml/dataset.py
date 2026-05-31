"""
dataset.py — GPU-optimized data loading for NNUE training.

Loads CSV data (fen, score_cp [, wdl]) and converts to HalfKAv2 features.
Designed for high throughput with:
  - Pre-computed feature indices (stored as numpy arrays)
  - Batched sparse tensor construction on GPU
  - Multi-worker DataLoader compatible collate function
"""

import csv
import numpy as np
import chess
from typing import List, Tuple, Optional
from pathlib import Path

import torch
from torch.utils.data import Dataset, DataLoader

from .features import board_features
from .arch import INPUT_SIZE, piece_count_bucket


class NNUEDataset(Dataset):
    """
    NNUE training dataset from CSV files.

    Expected CSV columns: fen, score_cp [, wdl]
      - score_cp : centipawns from WHITE's perspective
      - wdl      : optional, 1.0=white win, 0.5=draw, 0.0=white loss

    Filtering:
      - |score| > score_cap are dropped (default 10000)
      - Positions in check are dropped
      - Positions with < 3 pieces are dropped

    Each sample is pre-processed to store:
      - white_features: np.array of active feature indices
      - black_features: np.array of active feature indices
      - stm: 0=white, 1=black
      - score: float (from STM perspective)
      - wdl: float (from STM perspective, if available)
      - bucket: int (output bucket from piece count)
    """

    def __init__(self, csv_path: str, max_samples: int = 0,
                 score_cap: int = 10000, require_wdl: bool = False):
        self.samples: List[dict] = []
        self.has_wdl = False
        
        n_read = 0
        n_filtered = 0
        n_errors = 0

        print(f"Loading dataset from {csv_path} ...")

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            
            # Detect columns
            col_fen = 0
            col_score = 1
            col_wdl = -1
            
            if header:
                header_lower = [h.strip().lower() for h in header]
                if 'fen' in header_lower:
                    col_fen = header_lower.index('fen')
                if 'score_cp' in header_lower:
                    col_score = header_lower.index('score_cp')
                elif 'score' in header_lower:
                    col_score = header_lower.index('score')
                if 'wdl' in header_lower:
                    col_wdl = header_lower.index('wdl')
                    self.has_wdl = True
                
                # Check if header is actually data (no 'fen' column name)
                if not any(h in header_lower for h in ('fen', 'score_cp', 'score')):
                    # Header might be data, try parsing
                    self._try_add_row(header, col_fen, col_score, col_wdl, score_cap)
                    n_read += 1

            for row in reader:
                n_read += 1
                result = self._try_add_row(row, col_fen, col_score, col_wdl, score_cap)
                if result == 'filtered':
                    n_filtered += 1
                elif result == 'error':
                    n_errors += 1
                
                if max_samples > 0 and len(self.samples) >= max_samples:
                    break
                
                if n_read % 500_000 == 0:
                    print(f"  ... {n_read:,} read, {len(self.samples):,} kept")

        if n_filtered > 0:
            pct = 100.0 * n_filtered / max(n_read, 1)
            print(f"  Filtered {n_filtered:,} / {n_read:,} ({pct:.1f}%) positions")
        if n_errors > 0:
            print(f"  Skipped {n_errors:,} positions due to errors")
        print(f"  Loaded {len(self.samples):,} samples (has_wdl={self.has_wdl})")

    def _try_add_row(self, row, col_fen, col_score, col_wdl, score_cap) -> str:
        try:
            if len(row) <= max(col_fen, col_score):
                return 'error'
            
            fen = row[col_fen].strip()
            score_str = row[col_score].strip()
            
            try:
                score_white = int(score_str)
            except ValueError:
                score_white = int(float(score_str))
            
            if score_cap > 0 and abs(score_white) > score_cap:
                return 'filtered'
            
            # Parse WDL if available
            wdl_white = None
            if col_wdl >= 0 and len(row) > col_wdl:
                try:
                    wdl_white = float(row[col_wdl].strip())
                except (ValueError, IndexError):
                    pass
            
            # Parse FEN
            board = chess.Board(fen)
            
            # Skip invalid positions
            if board.is_check():
                return 'filtered'
            piece_count = len(board.piece_map())
            if piece_count < 3:
                return 'filtered'
            
            # Extract features
            white_feats, black_feats, pc = board_features(board)
            if not white_feats or not black_feats:
                return 'error'
            
            stm = 0 if board.turn == chess.WHITE else 1
            score_stm = score_white if stm == 0 else -score_white
            
            wdl_stm = None
            if wdl_white is not None:
                wdl_stm = wdl_white if stm == 0 else (1.0 - wdl_white)
            
            bucket = piece_count_bucket(pc)
            
            self.samples.append({
                'white_features': np.array(white_feats, dtype=np.int32),
                'black_features': np.array(black_feats, dtype=np.int32),
                'stm': stm,
                'score': float(score_stm),
                'wdl': float(wdl_stm) if wdl_stm is not None else 0.5,
                'bucket': bucket,
            })
            return 'ok'
            
        except Exception:
            return 'error'

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]


def collate_fn(batch: List[dict]) -> Tuple[torch.Tensor, ...]:
    """
    Sparse collate — returns padded index tensors instead of dense 40960-wide tensors.
    Same 8-tuple format as binary_collate_fn in data.py.

    Returns:
        (white_indices, white_counts, black_indices, black_counts, stm, scores, wdl, buckets)
        white/black_indices: (B, MAX_FEATS) int32  — active feature indices, zero-padded
        white/black_counts:  (B,) int32            — # valid indices per sample
        stm, buckets: (B,) long
        scores, wdl: (B,) float32
    """
    import numpy as np
    B = len(batch)
    MAX_FEATS = 32

    wi = np.zeros((B, MAX_FEATS), dtype=np.int32)
    bi = np.zeros((B, MAX_FEATS), dtype=np.int32)
    nw = np.zeros(B, dtype=np.int32)
    nb = np.zeros(B, dtype=np.int32)
    stm     = torch.zeros(B, dtype=torch.long)
    scores  = torch.zeros(B, dtype=torch.float32)
    wdl     = torch.zeros(B, dtype=torch.float32)
    buckets = torch.zeros(B, dtype=torch.long)

    for i, sample in enumerate(batch):
        wf = sample['white_features']
        bf = sample['black_features']
        nw[i] = min(len(wf), MAX_FEATS)
        nb[i] = min(len(bf), MAX_FEATS)
        wi[i, :nw[i]] = wf[:nw[i]]
        bi[i, :nb[i]] = bf[:nb[i]]
        stm[i]     = sample['stm']
        scores[i]  = sample['score']
        wdl[i]     = sample['wdl']
        buckets[i] = sample['bucket']

    return (torch.from_numpy(wi),
            torch.from_numpy(nw),
            torch.from_numpy(bi),
            torch.from_numpy(nb),
            stm, scores, wdl, buckets)


def collate_fn_sparse(batch: List[dict]) -> Tuple:
    """
    Sparse collate — returns index lists instead of dense tensors.
    Much more memory-efficient for the 40960-wide input.
    
    Returns:
        (white_indices, black_indices, stm, scores, wdl, buckets)
        white/black_indices: (N, 2) long tensor of (batch_idx, feature_idx)
    """
    B = len(batch)
    
    white_idx_list = []
    black_idx_list = []
    stm     = torch.zeros(B, dtype=torch.long)
    scores  = torch.zeros(B, dtype=torch.float32)
    wdl     = torch.zeros(B, dtype=torch.float32)
    buckets = torch.zeros(B, dtype=torch.long)

    for i, sample in enumerate(batch):
        for idx in sample['white_features']:
            white_idx_list.append((i, idx))
        for idx in sample['black_features']:
            black_idx_list.append((i, idx))
        stm[i]     = sample['stm']
        scores[i]  = sample['score']
        wdl[i]     = sample['wdl']
        buckets[i] = sample['bucket']

    # Build sparse COO tensors
    if white_idx_list:
        wi = torch.tensor(white_idx_list, dtype=torch.long)
        wv = torch.ones(wi.size(0), dtype=torch.float32)
        white_sparse = torch.sparse_coo_tensor(wi.t(), wv, size=(B, INPUT_SIZE))
    else:
        white_sparse = torch.sparse_coo_tensor(size=(B, INPUT_SIZE))

    if black_idx_list:
        bi = torch.tensor(black_idx_list, dtype=torch.long)
        bv = torch.ones(bi.size(0), dtype=torch.float32)
        black_sparse = torch.sparse_coo_tensor(bi.t(), bv, size=(B, INPUT_SIZE))
    else:
        black_sparse = torch.sparse_coo_tensor(size=(B, INPUT_SIZE))

    return white_sparse, black_sparse, stm, scores, wdl, buckets
