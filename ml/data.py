"""
data.py — Fast binary dataset for NNUE training.

Replaces the FEN-parsing CSV loader with a precomputed binary format.
Features are computed ONCE during prep (tools/prep_data.py), stored as
136-byte fixed-width records in a numpy memmap, and loaded at near-zero
cost every epoch.

Binary record layout (RECORD_DTYPE, 136 bytes):
  int16         score           score_cp from white's perspective
  float16       wdl             WDL probability, white's perspective (0.0–1.0)
  uint8         stm             0=white to move, 1=black
  uint8         bucket          output bucket (0–7, from piece count)
  uint8         n_white         # active white features (≤30)
  uint8         n_black         # active black features (≤30)
  uint16[32]    white_feats     active white feature indices, zero-padded
  uint16[32]    black_feats     active black feature indices, zero-padded

Performance vs CSV loader:
  - Dataset init: ~0.01s (mmap open vs minutes of FEN parsing)
  - Per-epoch collate: vectorized numpy scatter — no per-sample Python loops
  - Memory: O(file_size) resident — no Python objects per sample
"""

from __future__ import annotations
import os
import time
import numpy as np
import torch
from torch.utils.data import Dataset
from pathlib import Path
from typing import Tuple

from .arch import INPUT_SIZE

# ── Record format ─────────────────────────────────────────────────────────

MAX_FEATS = 32   # padded feature slots per perspective (max pieces = 30)

RECORD_DTYPE = np.dtype([
    ('score',       '<i2'),           # int16:    score_cp (white's perspective)
    ('wdl',         '<f2'),           # float16:  WDL probability (white's perspective)
    ('stm',         'u1'),            # uint8:    0=white, 1=black
    ('bucket',      'u1'),            # uint8:    output bucket index 0–7
    ('n_white',     'u1'),            # uint8:    # active white features
    ('n_black',     'u1'),            # uint8:    # active black features
    ('white_feats', '<u2', (MAX_FEATS,)),  # uint16[32]: white feature indices
    ('black_feats', '<u2', (MAX_FEATS,)),  # uint16[32]: black feature indices
])
# Verify: 2+2+1+1+1+1+64+64 = 136 bytes
RECORD_SIZE = RECORD_DTYPE.itemsize  # 136


# ── File header ───────────────────────────────────────────────────────────

HEADER_MAGIC   = b'NNUE_BIN'   # 8 bytes
HEADER_VERSION = 1             # increment if format changes
HEADER_SIZE    = 32            # bytes reserved for header


def _record_level_split(
    total: int, records_per_chunk: int, val_frac: float,
    val_seed: int | None, is_val: bool
) -> tuple[list[tuple[int, int]], int, int]:
    """
    Compute a (chunk_list, n_train, n_val) tuple using a **record-level** split.

    Unlike the old chunk-level split (which rounded to the nearest whole chunk
    boundary and broke with files that fit in a single chunk), this function
    always computes n_train / n_val at the record level first and then builds
    the chunk_list to exactly cover those records.

    With val_seed: chunks are shuffled for a representative random val set;
    the final chunk in the train allocation is trimmed to hit the exact
    n_train record count (never wastes records by rounding up to a chunk).

    Without val_seed: simple sequential split — train = [0, n_train),
    val = [n_train, total).
    """
    if total == 0:
        return [], 0, 0

    n_val   = max(1, int(total * val_frac))
    n_train = total - n_val
    if n_train <= 0:
        raise ValueError(
            f"Dataset has {total} records but val_frac={val_frac} leaves 0 for training. "
            f"Use a larger dataset or a smaller val_split."
        )

    if val_seed is not None:
        rng        = np.random.RandomState(val_seed)
        all_starts = list(map(int, np.arange(0, total, records_per_chunk)))
        rng.shuffle(all_starts)

        # Greedily assign whole chunks to train; split the boundary chunk to
        # hit the exact n_train record count; remainder → val.
        train_chunks: list[tuple[int, int]] = []
        val_chunks:   list[tuple[int, int]] = []
        train_remaining = n_train
        val_remaining   = n_val
        for s in all_starts:
            csize = int(min(s + records_per_chunk, total) - s)
            if train_remaining > 0:
                take_train = min(csize, train_remaining)
                train_chunks.append((s, take_train))
                train_remaining -= take_train
                leftover = csize - take_train
                if leftover > 0 and val_remaining > 0:
                    take_val = min(leftover, val_remaining)
                    val_chunks.append((s + take_train, take_val))
                    val_remaining -= take_val
            elif val_remaining > 0:
                take_val = min(csize, val_remaining)
                val_chunks.append((s, take_val))
                val_remaining -= take_val

        actual_train = n_train - train_remaining
        actual_val   = n_val   - val_remaining
        selected     = val_chunks if is_val else train_chunks
        return selected, actual_train, actual_val
    else:
        # Sequential: train = [0, n_train), val = [n_train, total)
        start0 = n_train if is_val else 0
        n_use  = n_val   if is_val else n_train
        end0   = start0 + n_use
        chunks = []
        for s in range(start0, end0, records_per_chunk):
            s     = int(s)
            csize = int(min(s + records_per_chunk, end0) - s)
            if csize > 0:
                chunks.append((s, csize))
        return chunks, n_train, n_val


def _write_header(f, n_records: int, input_size: int = INPUT_SIZE) -> None:
    """Write a fixed 32-byte header to an open binary file."""
    import struct
    header = struct.pack('<8sIII12x',
                         HEADER_MAGIC,
                         HEADER_VERSION,
                         n_records,
                         input_size)
    assert len(header) == HEADER_SIZE
    f.write(header)


def _read_header(path: str) -> dict:
    """Read and validate the 32-byte header, return metadata dict."""
    import struct
    with open(path, 'rb') as f:
        raw = f.read(HEADER_SIZE)
    magic, version, n_records, input_size = struct.unpack('<8sIII12x', raw)
    if magic != HEADER_MAGIC:
        raise ValueError(f"Not a binary NNUE dataset: magic={magic!r}")
    if version != HEADER_VERSION:
        raise ValueError(f"Version mismatch: got {version}, expected {HEADER_VERSION}")
    if input_size != INPUT_SIZE:
        raise ValueError(f"INPUT_SIZE mismatch: file={input_size}, compiled={INPUT_SIZE}")
    return {'n_records': n_records, 'input_size': input_size, 'version': version}


# ── Dataset ───────────────────────────────────────────────────────────────

class BinaryNNUEDataset(Dataset):
    """
    Memory-mapped binary NNUE dataset.

    Dataset init is ~instant (just mmap open).
    __getitem__ is a numpy structured array index — no Python overhead.
    Pairs with binary_collate_fn for fast vectorized batching.

    preload=True (default): copies the full data into a regular numpy array
    in RAM for fast random access during training. Adds ~5-10s init time
    but eliminates page-fault latency during shuffled iteration.
    """

    def __init__(self, path: str, max_samples: int = 0, preload: bool = True):
        self.path = str(path)
        meta = _read_header(self.path)
        total_records = meta['n_records']

        n = min(max_samples, total_records) if max_samples > 0 else total_records
        self.n_records = n

        # Always open as memmap first
        mmap = np.memmap(self.path, dtype=RECORD_DTYPE, mode='r',
                         offset=HEADER_SIZE, shape=(total_records,))

        if preload:
            mb = n * RECORD_SIZE / 1024**2
            print(f"  Preloading {mb:.1f} MB into RAM ...", end='', flush=True)
            t0 = time.time()
            self._mmap = np.array(mmap[:n])  # copy to contiguous RAM array
            print(f" done in {time.time()-t0:.1f}s")
        else:
            self._mmap = mmap

        print(f"Binary dataset:  {self.path}")
        print(f"  Records: {self.n_records:,}  Record size: {RECORD_SIZE} bytes"
              f"  Total: {self.n_records * RECORD_SIZE / 1024**2:.1f} MB")

    def __len__(self) -> int:
        return self.n_records

    def __getitem__(self, idx: int) -> dict:
        r = self._mmap[idx]
        stm = int(r['stm'])
        # .bin stores score/WDL from WHITE's perspective; convert to STM's perspective.
        score = float(r['score']) if stm == 0 else -float(r['score'])
        wdl   = float(r['wdl'])  if stm == 0 else 1.0 - float(r['wdl'])
        return {
            'score':       score,
            'wdl':         wdl,
            'stm':         stm,
            'bucket':      int(r['bucket']),
            'n_white':     int(r['n_white']),
            'n_black':     int(r['n_black']),
            'white_feats': r['white_feats'].copy(),  # (MAX_FEATS,) uint16
            'black_feats': r['black_feats'].copy(),  # (MAX_FEATS,) uint16
        }


# ── Collate ───────────────────────────────────────────────────────────────

def binary_collate_fn(batch: list) -> Tuple[torch.Tensor, ...]:
    """
    Sparse collate — returns padded index tensors instead of dense 40960-wide tensors.
    Reduces per-batch CPU allocation from ~640 MB to ~0.2 MB; the embedding
    lookup (scatter into FT) happens on the GPU inside model.forward().

    Returns:
      (white_indices, white_counts, black_indices, black_counts, stm, scores, wdl, buckets)
      white/black_indices: (B, MAX_FEATS) int32  — active feature indices, zero-padded
      white/black_counts:  (B,) int32            — # valid indices per sample (≤30)
      stm, buckets: (B,) long
      scores, wdl: (B,) float32
    """
    wi = np.stack([s['white_feats'] for s in batch]).astype(np.int32)  # (B, MAX_FEATS)
    bi = np.stack([s['black_feats'] for s in batch]).astype(np.int32)
    nw = np.array([s['n_white'] for s in batch], dtype=np.int32)
    nb = np.array([s['n_black'] for s in batch], dtype=np.int32)

    stm     = torch.tensor([s['stm']    for s in batch], dtype=torch.long)
    scores  = torch.tensor([s['score']  for s in batch], dtype=torch.float32)
    wdl     = torch.tensor([s['wdl']    for s in batch], dtype=torch.float32)
    buckets = torch.tensor([s['bucket'] for s in batch], dtype=torch.long)

    return (torch.from_numpy(wi),
            torch.from_numpy(nw),
            torch.from_numpy(bi),
            torch.from_numpy(nb),
            stm, scores, wdl, buckets)


# ── Fast vectorized batch loader ──────────────────────────────────────────

class FastBinaryLoader:
    """
    Drop-in replacement for DataLoader when using preloaded BinaryNNUEDataset.

    Bypasses DataLoader + per-sample __getitem__ + collate entirely.
    Each batch is produced by a SINGLE numpy fancy-index on the record array:
        r = arr[batch_idx]       # one call → (B,) structured array
        wi = r['white_feats']    # (B, 32) uint16 view — zero copy

    Speedup vs DataLoader(num_workers=0): ~10-50× (no Python loop per sample).

    Args:
        arr:        numpy structured array of RECORD_DTYPE (preloaded into RAM)
        batch_size: samples per batch
        shuffle:    randomise order each epoch
    """

    def __init__(self, arr: np.ndarray, batch_size: int, shuffle: bool = True,
                 train_score_cap: int = 0):
        self._arr = arr
        self._n = len(arr)
        self._bs = batch_size
        self._shuffle = shuffle
        self._train_score_cap = train_score_cap
        self._sample_probs: np.ndarray | None = None  # set by set_bucket_weights()

    def set_bucket_weights(self, bucket_weights: np.ndarray | None) -> None:
        """Set per-bucket sampling weights for weighted random sampling.

        Each record's probability is proportional to bucket_weights[record.bucket].
        Call once per epoch (before iterating) to implement curriculum decay.
        Pass None to revert to uniform sampling.

        Args:
            bucket_weights: float array of length OUTPUT_BUCKETS, or None.
        """
        if bucket_weights is None:
            self._sample_probs = None
            return
        buckets = self._arr['bucket'].astype(np.int32)
        w = np.asarray(bucket_weights, dtype=np.float64)[buckets]
        w = np.maximum(w, 1e-12)
        self._sample_probs = w / w.sum()

    def __len__(self) -> int:
        if self._n <= 0:
            return 0
        return (self._n + self._bs - 1) // self._bs

    def __iter__(self):
        n, bs = self._n, self._bs
        if self._sample_probs is not None:
            # Weighted sampling with replacement — preserves epoch size
            idx = np.random.choice(n, size=n, replace=True, p=self._sample_probs)
        elif self._shuffle:
            idx = np.random.permutation(n)
        else:
            idx = np.arange(n)

        for start in range(0, n, bs):
            r = self._arr[idx[start:start + bs]]   # (B,) structured records
            if len(r) == 0:
                continue

            # .bin stores score and WDL from WHITE's perspective.
            # Model outputs from STM's perspective (friendly first).
            # Flip sign for black-to-move so labels match model output.
            stm_arr   = r['stm'].astype(np.float32)          # 0=white, 1=black
            score_arr = r['score'].astype(np.float32)         # white-perspective cp
            wdl_arr   = r['wdl'].astype(np.float32)           # white win probability
            score_stm = np.where(stm_arr == 1, -score_arr, score_arr)   # STM-perspective
            if self._train_score_cap > 0:
                score_stm = np.clip(score_stm, -self._train_score_cap, self._train_score_cap)
                wdl_stm = 1.0 / (1.0 + np.exp(-score_stm / 600.0))     # STM win prob (recalc)
            else:
                wdl_stm   = np.where(stm_arr == 1, 1.0 - wdl_arr, wdl_arr) # STM win prob

            yield (
                torch.from_numpy(r['white_feats'].astype(np.int32)),  # (B, 32)
                torch.from_numpy(r['n_white'].astype(np.int32)),       # (B,)
                torch.from_numpy(r['black_feats'].astype(np.int32)),   # (B, 32)
                torch.from_numpy(r['n_black'].astype(np.int32)),       # (B,)
                torch.from_numpy(r['stm'].astype(np.int64)),           # (B,)
                torch.from_numpy(score_stm),                           # (B,) STM-perspective
                torch.from_numpy(wdl_stm),                             # (B,) STM win prob
                torch.from_numpy(r['bucket'].astype(np.int64)),        # (B,)
            )




class ChunkedBinaryLoader:
    """
    Memory-efficient loader for large datasets that don't fit in RAM.

    Instead of preloading the entire file, keeps only a memmap open and
    loads one chunk at a time per epoch. Peak RAM usage is ~chunk_gb GB
    instead of the full file size.

    Shuffle strategy:
      - Chunk order is randomised each epoch
      - Records within each chunk are shuffled before batching
    This gives equivalent training quality to full-file shuffle for
    datasets where in-game position order has already been broken by
    the parallel extraction (chunks come from different file regions).

    Args:
        path:       path to .bin file
        batch_size: samples per batch
        chunk_gb:   approximate RAM to use per chunk (default 1.5 GB)
        val_frac:   fraction of records reserved for validation
        is_val:     if True, iterate only the validation slice
    """

    def __init__(self, path: str, batch_size: int, chunk_gb: float = 0.5,
                 val_frac: float = 0.1, is_val: bool = False, silent: bool = False,
                 val_seed: int | None = None, train_score_cap: int = 0,
                 score_filter_max: int = 0):
        self._path            = str(path)
        self._bs              = batch_size
        self._is_val          = is_val
        self._train_score_cap = train_score_cap
        self._score_filter_max = score_filter_max

        # Read header only — no memmap kept open (memmap accumulates all file
        # pages in the process working set after one full epoch; direct file
        # reads avoid this: OS file cache is not attributed to the Python process)
        meta = _read_header(self._path)
        total = meta['n_records']

        # Chunk size based on total dataset so buf is self-consistent across both splits.
        if total == 0:
            self._chunk_list = []
            self._n = 0
            self._n_chunks = 0
            self._n_train = 0
            self._n_val = 0
            self._buf = np.empty(1, dtype=RECORD_DTYPE)
            self._chunk_size = 1
            return
        records_per_chunk = min(
            total,
            max(batch_size, int(chunk_gb * 1024**3 / RECORD_SIZE))
        )
        self._chunk_size = records_per_chunk

        chunk_list, self._n_train, self._n_val = _record_level_split(
            total, records_per_chunk, val_frac, val_seed, is_val
        )
        self._chunk_list = chunk_list
        self._n          = sum(c[1] for c in chunk_list)
        self._n_chunks   = len(chunk_list)

        # Pre-allocate ONE reusable buffer — never freed/reallocated during training.
        # readinto() fills it directly, np.random.shuffle shuffles in-place.
        self._buf      = np.empty(records_per_chunk, dtype=RECORD_DTYPE)
        self._megabuf  = None  # unused; kept for forward compat

        mb = self._n * RECORD_SIZE / 1024**2
        if not silent:
            label = 'Val' if is_val else 'Train'
            print(f"{label} dataset: {self._n:,} records "
                  f"({mb:.0f} MB on disk)  chunks={self._n_chunks}  "
                  f"chunk_size={records_per_chunk:,}")

    def __len__(self) -> int:
        """Total batches across all chunks (approximate, used for progress display)."""
        if self._n <= 0:
            return 0
        return (self._n + self._bs - 1) // self._bs

    def _read_chunk_into(self, buf_view, file_record_start: int) -> None:
        """Read exactly len(buf_view) records from the file starting at the given record index."""
        file_offset = HEADER_SIZE + file_record_start * RECORD_SIZE
        with open(self._path, 'rb') as fh:
            fh.seek(file_offset)
            fh.readinto(buf_view)

    @staticmethod
    def _yield_tensors(buf, train_score_cap: int = 0):
        """Convert a numpy record array slice to the tuple of tensors expected by the trainer."""
        stm_arr   = buf['stm'].astype(np.float32)
        score_arr = buf['score'].astype(np.float32)
        wdl_arr   = buf['wdl'].astype(np.float32)
        # .bin stores score/WDL from WHITE's perspective; flip for black-to-move.
        score_stm = np.where(stm_arr == 1, -score_arr, score_arr)
        if train_score_cap > 0:
            score_stm = np.clip(score_stm, -train_score_cap, train_score_cap)
            wdl_stm = 1.0 / (1.0 + np.exp(-score_stm / 600.0))     # STM win prob (recalc)
        else:
            wdl_stm = np.where(stm_arr == 1, 1.0 - wdl_arr, wdl_arr)
        return (
            torch.from_numpy(buf['white_feats'].astype(np.int32)),
            torch.from_numpy(buf['n_white'].astype(np.int32)),
            torch.from_numpy(buf['black_feats'].astype(np.int32)),
            torch.from_numpy(buf['n_black'].astype(np.int32)),
            torch.from_numpy(buf['stm'].astype(np.int64)),
            torch.from_numpy(score_stm),
            torch.from_numpy(wdl_stm),
            torch.from_numpy(buf['bucket'].astype(np.int64)),
        )

    def __iter__(self):
        bs     = self._bs
        chunks = list(self._chunk_list)

        # Randomise chunk visit order each epoch (train only).
        if not self._is_val:
            np.random.shuffle(chunks)

        for file_record_start, csize in chunks:
            buf = self._buf[:csize]
            self._read_chunk_into(buf, file_record_start)
            if not self._is_val:
                np.random.shuffle(buf)
            # Score filter: drop positions outside the training score range.
            # Applied after shuffle so filtered-out positions don't bias order.
            if self._score_filter_max > 0:
                buf = buf[np.abs(buf['score']) <= self._score_filter_max]
            for start in range(0, len(buf), bs):
                r = buf[start:start + bs]
                if len(r) > 0:
                    yield self._yield_tensors(r, self._train_score_cap)


class InterleavedChunkedLoader:
    """
    Interleaved variant of ChunkedBinaryLoader.

    Instead of yielding all batches from one chunk before moving to the next,
    loads K chunks simultaneously into a single megabuffer, shuffles the
    combined records, then yields batches from the mix.

    This ensures every batch contains records from K different source chunks,
    breaking the within-chunk homogeneity that causes gradient cancellation
    when source chunks are from different game databases with different score
    distributions.

    Peak RAM: interleave_k x chunk_gb (default 8 x 0.5 GB = 4 GB).
    """

    def __init__(self, path: str, batch_size: int, chunk_gb: float = 0.5,
                 val_frac: float = 0.1, is_val: bool = False, silent: bool = False,
                 val_seed: int | None = None, interleave_k: int = 8,
                 train_score_cap: int = 0, score_filter_max: int = 0):
        self._path             = str(path)
        self._bs               = batch_size
        self._is_val           = is_val
        self._k                = interleave_k
        self._train_score_cap  = train_score_cap
        self._score_filter_max = score_filter_max

        meta  = _read_header(self._path)
        total = meta['n_records']

        records_per_chunk = min(
            total,
            max(batch_size, int(chunk_gb * 1024**3 / RECORD_SIZE))
        )
        self._chunk_size = records_per_chunk

        chunk_list, self._n_train, self._n_val = _record_level_split(
            total, records_per_chunk, val_frac, val_seed, is_val
        )

        self._chunk_list = chunk_list
        self._n          = sum(c[1] for c in chunk_list)
        self._n_chunks   = len(chunk_list)

        # Pre-allocate a megabuffer large enough for K chunks.
        megabuf_size  = records_per_chunk * interleave_k
        self._megabuf = np.empty(megabuf_size, dtype=RECORD_DTYPE)

        mb     = self._n * RECORD_SIZE / 1024**2
        buf_mb = megabuf_size * RECORD_SIZE / 1024**2
        if not silent:
            label = 'Val' if is_val else 'Train'
            print(f"{label} dataset: {self._n:,} records "
                  f"({mb:.0f} MB on disk)  chunks={self._n_chunks}  "
                  f"chunk_size={records_per_chunk:,}  interleave_k={interleave_k}  "
                  f"megabuf={buf_mb:.0f} MB")

    def __len__(self) -> int:
        if self._n <= 0:
            return 0
        return (self._n + self._bs - 1) // self._bs

    def _read_chunk_into(self, buf_view, file_record_start: int) -> None:
        file_offset = HEADER_SIZE + file_record_start * RECORD_SIZE
        with open(self._path, 'rb') as fh:
            fh.seek(file_offset)
            fh.readinto(buf_view)

    @staticmethod
    def _yield_tensors(buf, train_score_cap: int = 0):
        stm_arr   = buf['stm'].astype(np.float32)
        score_arr = buf['score'].astype(np.float32)
        wdl_arr   = buf['wdl'].astype(np.float32)
        score_stm = np.where(stm_arr == 1, -score_arr, score_arr)
        if train_score_cap > 0:
            score_stm = np.clip(score_stm, -train_score_cap, train_score_cap)
            wdl_stm = 1.0 / (1.0 + np.exp(-score_stm / 600.0))     # STM win prob (recalc)
        else:
            wdl_stm = np.where(stm_arr == 1, 1.0 - wdl_arr, wdl_arr)
        return (
            torch.from_numpy(buf['white_feats'].astype(np.int32)),
            torch.from_numpy(buf['n_white'].astype(np.int32)),
            torch.from_numpy(buf['black_feats'].astype(np.int32)),
            torch.from_numpy(buf['n_black'].astype(np.int32)),
            torch.from_numpy(buf['stm'].astype(np.int64)),
            torch.from_numpy(score_stm),
            torch.from_numpy(wdl_stm),
            torch.from_numpy(buf['bucket'].astype(np.int64)),
        )

    def __iter__(self):
        bs     = self._bs
        k      = self._k
        chunks = list(self._chunk_list)

        if not self._is_val:
            np.random.shuffle(chunks)

        # Process chunks in groups of K: load all K into megabuffer, shuffle, yield batches.
        for group_start in range(0, len(chunks), k):
            group          = chunks[group_start:group_start + k]
            total_in_group = sum(c[1] for c in group)
            mb_view        = self._megabuf[:total_in_group]

            offset = 0
            for file_record_start, csize in group:
                self._read_chunk_into(mb_view[offset:offset + csize], file_record_start)
                offset += csize

            if not self._is_val:
                np.random.shuffle(mb_view)

            # Score filter: drop positions outside the training score range.
            if self._score_filter_max > 0:
                mb_view = mb_view[np.abs(mb_view['score']) <= self._score_filter_max]

            for start in range(0, len(mb_view), bs):
                r = mb_view[start:start + bs]
                if len(r) > 0:
                    yield self._yield_tensors(r, self._train_score_cap)


class MultiChunkedBinaryLoader:
    """
    Lazy wrapper around multiple ChunkedBinaryLoaders.

    Files are opened one at a time inside __iter__ and discarded afterwards,
    so peak RAM is exactly ONE chunk buffer (~chunk_gb GB) regardless of how
    many files are in the list.  This is critical when training on 100+ .bin
    files — eagerly pre-allocating all buffers would exhaust RAM.

    Each file's loader shuffles its own chunk order and in-chunk records.
    This wrapper shuffles the file visit order each epoch for an even mix.
    """

    def __init__(self, paths: list, batch_size: int, chunk_gb: float = 0.5,
                 val_frac: float = 0.1, is_val: bool = False,
                 train_score_cap: int = 0):
        self._paths     = [str(p) for p in paths]
        self._bs        = batch_size
        self._chunk_gb  = chunk_gb
        self._val_frac  = val_frac
        self._is_val    = is_val
        self._train_score_cap = train_score_cap

        # Compute total batch count by reading only file headers (no buffers).
        # Skip files with 0 records (e.g. partially-written converting files).
        total_batches = 0
        valid_paths = []
        for p in self._paths:
            meta    = _read_header(p)
            n       = meta['n_records']
            if n == 0:
                continue
            valid_paths.append(p)
            n_val   = max(1, int(n * val_frac))
            n_use   = n_val if is_val else (n - n_val)
            if n_use > 0:
                total_batches += (n_use + batch_size - 1) // batch_size
        self._paths = valid_paths
        self._len = total_batches

        label      = 'Val' if is_val else 'Train'
        total_gb   = sum(os.path.getsize(p) / 1024**3 for p in self._paths)
        print(f"{label} MultiLoader: {len(self._paths)} files, "
              f"{total_gb:.1f} GB total, ~{total_batches:,} batches/epoch "
              f"(lazy — 1 x {chunk_gb:.1f} GB buffer at a time)")

    def __len__(self) -> int:
        return self._len

    def __iter__(self):
        import random
        paths = list(self._paths)
        random.shuffle(paths)
        for path in paths:
            # Lazily allocate one buffer, stream it, then release immediately.
            loader = ChunkedBinaryLoader(
                path, self._bs,
                chunk_gb=self._chunk_gb,
                val_frac=self._val_frac,
                is_val=self._is_val,
                silent=True,   # suppress per-file prints (143 files × N epochs)
                train_score_cap=self._train_score_cap,
            )
            yield from loader
            del loader  # explicitly release the chunk buffer


class BinaryWriter:
    """
    Streaming writer for the binary dataset format.
    Used by prep_data.py (and eventually the SF labeling pipeline) to
    build binary files without loading everything into memory first.

    Usage:
        with BinaryWriter('output.bin') as w:
            w.write_batch(records_array)  # np.ndarray of RECORD_DTYPE
        # Header is patched with final record count on close.
    """

    def __init__(self, path: str, buffer_size: int = 65536):
        self.path = path
        self._f = open(path, 'wb')
        self._n_written = 0
        # Write placeholder header (will be patched on close)
        _write_header(self._f, 0)

    def write_batch(self, records: np.ndarray) -> None:
        """Write a batch of RECORD_DTYPE records."""
        assert records.dtype == RECORD_DTYPE, f"Expected {RECORD_DTYPE}, got {records.dtype}"
        self._f.write(records.tobytes())
        self._n_written += len(records)

    def close(self) -> int:
        """Patch header with final count and close. Returns n_records."""
        import struct
        self._f.seek(0)
        _write_header(self._f, self._n_written)
        self._f.close()
        return self._n_written

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Inspection helpers ────────────────────────────────────────────────────

def inspect(path: str, n: int = 5) -> None:
    """Print summary stats and first n records from a binary file."""
    meta = _read_header(path)
    ds = BinaryNNUEDataset(path, preload=False)  # no need to load all for inspection
    mmap = ds._mmap

    scores = mmap['score'].astype(np.float32)
    wdls   = mmap['wdl'].astype(np.float32)
    nw     = mmap['n_white'].astype(np.int32)
    nb     = mmap['n_black'].astype(np.int32)

    print(f"\nFile: {path}")
    print(f"Records:     {meta['n_records']:,}")
    print(f"Score range: [{scores.min():.0f}, {scores.max():.0f}] cp   "
          f"mean={scores.mean():.1f}  std={scores.std():.1f}")
    print(f"WDL range:   [{wdls.min():.3f}, {wdls.max():.3f}]   mean={wdls.mean():.3f}")
    print(f"Features/pos: white={nw.mean():.1f}±{nw.std():.1f}  black={nb.mean():.1f}±{nb.std():.1f}")
    print(f"STM:         white={100*(mmap['stm']==0).mean():.1f}%  black={100*(mmap['stm']==1).mean():.1f}%")
    buckets, counts = np.unique(mmap['bucket'], return_counts=True)
    print(f"Buckets:     " + "  ".join(f"{b}:{c:,}" for b, c in zip(buckets, counts)))

    print(f"\nFirst {n} records:")
    for i in range(min(n, meta['n_records'])):
        r = mmap[i]
        print(f"  [{i}] score={r['score']:+5d} wdl={float(r['wdl']):.2f} "
              f"stm={'W' if r['stm']==0 else 'B'} bucket={r['bucket']} "
              f"nw={r['n_white']} nb={r['n_black']}")


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m ml.data <file.bin> [n_rows]")
        sys.exit(1)
    inspect(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5)
