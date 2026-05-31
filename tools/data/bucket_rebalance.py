"""
bucket_rebalance.py â€” Downsample over-represented output buckets.

Middlegame buckets (1-3) typically contain 3-5Ã— more records than endgame
buckets (0) or large-material buckets (7).  This imbalance causes the
network's endgame and opening output heads to be undertrained relative to
their frequency during actual play.

Strategy: stream the file once, collect per-bucket record indices, then
for each bucket keep at most `cap_factor Ã— min_bucket_count` records,
chosen uniformly at random.  Writes a new .bin in the same 136-byte format.

Usage:
    python tools/bucket_rebalance.py data/processed/v2-consistency.bin \\
        --output data/processed/v2-rebalanced.bin \\
        --cap-factor 3

    # Hard cap per-bucket (alternative to cap-factor):
    python tools/bucket_rebalance.py data/processed/v2-consistency.bin \\
        --output data/processed/v2-rebalanced.bin \\
        --max-per-bucket 50000000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.data import RECORD_DTYPE, HEADER_SIZE, BinaryWriter, _read_header

OUTPUT_BUCKETS = 8


def main() -> None:
    ap = argparse.ArgumentParser(
        description='Downsample over-represented output buckets to balance training pressure'
    )
    ap.add_argument('input', help='Source .bin file (not modified)')
    ap.add_argument('--output', required=True, help='Output rebalanced .bin path')
    ap.add_argument('--cap-factor', type=float, default=3.0,
                    help='Cap each bucket at cap_factor Ã— min_bucket_count (default: 3.0)')
    ap.add_argument('--max-per-bucket', type=int, default=None,
                    help='Hard cap per bucket; overrides --cap-factor if set')
    ap.add_argument('--seed', type=int, default=42, help='RNG seed')
    ap.add_argument('--chunk-records', type=int, default=2_000_000,
                    help='Records per streaming chunk (default: 2M)')
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        raise SystemExit(f'Input not found: {src}')

    meta = _read_header(str(src))
    n_total = meta['n_records']
    print(f'Source: {src.name}  ({n_total:,} records)')

    rng = np.random.default_rng(args.seed)
    t0 = time.time()

    # â”€â”€ Pass 1: count records per bucket â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    arr = np.memmap(str(src), dtype=RECORD_DTYPE, mode='r', offset=HEADER_SIZE, shape=(n_total,))

    print('Pass 1: counting bucket sizes...')
    bucket_counts = np.zeros(OUTPUT_BUCKETS, dtype=np.int64)
    chunk = args.chunk_records
    for start in range(0, n_total, chunk):
        end = min(start + chunk, n_total)
        b = arr['bucket'][start:end].astype(np.int32)
        for bi in range(OUTPUT_BUCKETS):
            bucket_counts[bi] += int(np.sum(b == bi))

    min_count = int(bucket_counts[bucket_counts > 0].min())
    if args.max_per_bucket is not None:
        cap = args.max_per_bucket
    else:
        cap = int(args.cap_factor * min_count)

    print(f'\nBucket distribution (cap={cap:,}):')
    for b in range(OUTPUT_BUCKETS):
        keep = min(int(bucket_counts[b]), cap)
        ratio = keep / max(bucket_counts[b], 1) * 100
        print(f'  bucket {b}: {bucket_counts[b]:>12,}  â†’ keep {keep:>12,}  ({ratio:.1f}%)')
    print(f'  min bucket  : {min_count:,}')
    print(f'  cap         : {cap:,}')

    expected_out = sum(min(int(bucket_counts[b]), cap) for b in range(OUTPUT_BUCKETS))
    print(f'  expected out: {expected_out:,}  ({expected_out / n_total * 100:.1f}% of input)')

    # â”€â”€ Pass 2: collect per-bucket indices â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\nPass 2: collecting indices...')
    bucket_indices: list[list[int]] = [[] for _ in range(OUTPUT_BUCKETS)]
    for start in range(0, n_total, chunk):
        end = min(start + chunk, n_total)
        buckets_chunk = arr['bucket'][start:end].astype(np.int32)
        for b in range(OUTPUT_BUCKETS):
            mask = np.where(buckets_chunk == b)[0]
            if len(mask):
                bucket_indices[b].extend((start + mask).tolist())
        pct = end / n_total * 100
        print(f'  {end:,} / {n_total:,}  ({pct:.1f}%)', end='\r')
    print()

    # â”€â”€ Pass 3: sample and write â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\nPass 3: sampling and writing...')
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_written = 0
    with BinaryWriter(str(out_path)) as writer:
        for b in range(OUTPUT_BUCKETS):
            idxs = np.array(bucket_indices[b], dtype=np.int64)
            n_take = min(len(idxs), cap)
            if n_take == 0:
                print(f'  bucket {b}: empty â€” skipping')
                continue
            chosen = rng.choice(idxs, size=n_take, replace=False)
            chosen.sort()  # sequential mmap access
            # Write in sub-chunks to avoid large allocations
            sub = 500_000
            for s in range(0, len(chosen), sub):
                records = np.array(arr[chosen[s:s + sub]], copy=True)
                writer.write_batch(records)
            n_written += n_take
            print(f'  bucket {b}: {n_take:,} records written')

    elapsed = time.time() - t0
    size_gb = out_path.stat().st_size / 1e9
    print(f'\nDone in {elapsed:.0f}s')
    print(f'  output  : {out_path}  ({size_gb:.2f} GB)')
    print(f'  records : {n_written:,}  ({n_written / n_total * 100:.1f}% of input)')


if __name__ == '__main__':
    main()
