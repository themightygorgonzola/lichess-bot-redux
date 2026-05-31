"""
stratified_val_split.py â€” Build a stratified validation set from a .bin dataset.

Samples an equal number of records from each of the 8 output buckets so every
material-count head gets fair validation coverage.  The validation file is written
as a standard .bin compatible with --val-data in trainer.py.

The source file is NOT modified.

Usage:
    python tools/stratified_val_split.py data/processed/v2-consistency.bin \\
        --val-output data/processed/val-stratified.bin \\
        --n-per-bucket 31250 --seed 42
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
        description='Build a stratified validation set balanced across all 8 output buckets'
    )
    ap.add_argument('input', help='Source .bin file (not modified)')
    ap.add_argument('--val-output', required=True, help='Output validation .bin path')
    ap.add_argument('--n-per-bucket', type=int, default=31_250,
                    help='Records to sample per bucket (default: 31250 â†’ 250K total)')
    ap.add_argument('--seed', type=int, default=42, help='RNG seed for reproducibility')
    ap.add_argument('--chunk-records', type=int, default=2_000_000,
                    help='Records to stream per chunk (default: 2M)')
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        raise SystemExit(f'Input not found: {src}')

    meta = _read_header(str(src))
    n_total = meta['n_records']
    print(f'Source: {src.name}  ({n_total:,} records)')
    print(f'Target: {args.n_per_bucket:,} per bucket Ã— {OUTPUT_BUCKETS} = {args.n_per_bucket * OUTPUT_BUCKETS:,} total')

    rng = np.random.default_rng(args.seed)
    t0 = time.time()

    # â”€â”€ Pass 1: collect indices per bucket â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Stream through the file in chunks; keep track of which absolute record
    # indices belong to each bucket.  We use reservoir sampling so we never
    # need to hold all indices in memory at once (though for 500M records the
    # index array at 8 bytes each is ~4 GB â€” acceptable for a one-time run).
    # Reservoir size per bucket: n_per_bucket * 4 oversampling then downsample,
    # but for simplicity we just collect ALL indices then sample.

    arr = np.memmap(str(src), dtype=RECORD_DTYPE, mode='r', offset=HEADER_SIZE, shape=(n_total,))

    print('Pass 1: collecting indices per bucket...')
    bucket_indices: list[list[int]] = [[] for _ in range(OUTPUT_BUCKETS)]

    chunk = args.chunk_records
    for start in range(0, n_total, chunk):
        end = min(start + chunk, n_total)
        buckets = arr['bucket'][start:end].astype(np.int32)
        for b in range(OUTPUT_BUCKETS):
            mask = np.where(buckets == b)[0]
            if len(mask):
                bucket_indices[b].extend((start + mask).tolist())
        pct = end / n_total * 100
        elapsed = time.time() - t0
        print(f'  {end:,} / {n_total:,}  ({pct:.1f}%)  {elapsed:.0f}s', end='\r')

    print()

    # â”€â”€ Report bucket coverage â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\nBucket counts in source:')
    for b in range(OUTPUT_BUCKETS):
        n = len(bucket_indices[b])
        want = args.n_per_bucket
        status = 'OK' if n >= want else f'WARN: only {n:,} available'
        print(f'  bucket {b}: {n:,}  â†’ sample {min(n, want):,}  [{status}]')

    # â”€â”€ Pass 2: sample and write â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\nPass 2: sampling and writing...')
    val_out = Path(args.val_output)
    val_out.parent.mkdir(parents=True, exist_ok=True)

    n_written = 0
    with BinaryWriter(str(val_out)) as writer:
        for b in range(OUTPUT_BUCKETS):
            idxs = np.array(bucket_indices[b], dtype=np.int64)
            n_take = min(len(idxs), args.n_per_bucket)
            if n_take == 0:
                print(f'  bucket {b}: no records â€” skipping')
                continue
            chosen = rng.choice(idxs, size=n_take, replace=False)
            chosen.sort()  # sequential access is faster on mmap
            records = np.array(arr[chosen], copy=True)
            writer.write_batch(records)
            n_written += n_take
            print(f'  bucket {b}: wrote {n_take:,}')

    elapsed = time.time() - t0
    size_mb = val_out.stat().st_size / 1e6
    print(f'\nDone in {elapsed:.0f}s')
    print(f'  output : {val_out}  ({size_mb:.1f} MB)')
    print(f'  records: {n_written:,}')


if __name__ == '__main__':
    main()
