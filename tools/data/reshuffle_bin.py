#!/usr/bin/env python3
"""
tools/reshuffle_bin.py â€” True position-level shuffle of NNUE .bin dataset.

The existing shuffle_bin_dataset.py shuffles CHUNK ORDER but never mixes
records between chunks. If the file was assembled from multiple sources with
different score distributions, those distributions stay siloed inside their
chunks, causing per-epoch gradient variance in the trainer.

This script performs a proper two-pass external shuffle that assigns every
record a uniformly random position in the output, regardless of its original
location in the file.

Algorithm:
  Pass 1 (scatter): Read input sequentially. For each record, pick a random
                    bucket 0..N-1 and append the record to that bucket's temp
                    file. Pure sequential reads, N sequential appends.
  Pass 2 (gather):  For each bucket: load into RAM, numpy.shuffle in-place,
                    append to output. Pure sequential writes.

After reshuffling, every consecutive chunk of records in the output file
contains a representative sample from all regions of the input file, so
per-chunk score distributions are uniform. Val loss oscillation goes away.

Disk required: ~2Ã— file size (temp buckets + output). Both can be on the same
drive. Use --temp-dir to redirect temp files to a different volume.

Usage:
  python tools/reshuffle_bin.py data/processed/mean-alltime-dedup-shuffled.bin
  python tools/reshuffle_bin.py input.bin output.bin
  python tools/reshuffle_bin.py input.bin --inplace          # replaces input after verify
  python tools/reshuffle_bin.py input.bin --n-buckets 64     # explicit bucket count
"""

import sys
import os
import time
import shutil
import struct
import argparse
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml.data import (
    _read_header, _write_header,
    RECORD_DTYPE, RECORD_SIZE, HEADER_SIZE, HEADER_MAGIC, HEADER_VERSION,
)


# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def fmt_gb(n_bytes):
    return f'{n_bytes / 1024**3:.1f} GB'

def fmt_time(s):
    if s < 60:   return f'{s:.0f}s'
    if s < 3600: return f'{s / 60:.1f}m'
    return f'{s / 3600:.1f}h'

def fmt_rate(bytes_per_sec):
    gb = bytes_per_sec / 1024**3
    if gb >= 0.1:
        return f'{gb:.2f} GB/s'
    return f'{bytes_per_sec / 1024**2:.0f} MB/s'

def progress(label, done, total, t0, suffix=''):
    elapsed = time.time() - t0
    pct = done / total * 100 if total > 0 else 0
    rate = done / max(elapsed, 1e-9)
    eta  = (total - done) / max(rate, 1e-9)
    print(f'\r  {label}: {pct:5.1f}%  {fmt_gb(done)}/{fmt_gb(total)}'
          f'  {fmt_rate(rate)}  ETA {fmt_time(eta)}{suffix}   ',
          end='', flush=True)


# â”€â”€ Pass 1: scatter â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def scatter_pass(input_path, bucket_fhs, n_records, rng, read_chunk=2_000_000):
    """
    Read input sequentially, assign each record a random bucket, write to
    that bucket's file handle. Returns per-bucket record counts.
    """
    N = len(bucket_fhs)
    buf = np.empty(read_chunk, dtype=RECORD_DTYPE)
    bucket_counts = np.zeros(N, dtype=np.int64)
    written = 0
    t0 = time.time()
    total_bytes = n_records * RECORD_SIZE

    with open(input_path, 'rb') as fh:
        fh.seek(HEADER_SIZE)
        while written < n_records:
            size = min(read_chunk, n_records - written)
            view = buf[:size]
            fh.readinto(view)

            # Assign each record to a uniformly random bucket.
            assignments = rng.integers(0, N, size=size, dtype=np.uint8)

            # Sort by bucket so writes to each bucket file are sequential.
            sort_idx = np.argsort(assignments, kind='stable')
            sorted_asgn  = assignments[sort_idx]
            sorted_chunk = view[sort_idx]

            for b in range(N):
                lo = np.searchsorted(sorted_asgn, b,     side='left')
                hi = np.searchsorted(sorted_asgn, b + 1, side='left')
                if hi > lo:
                    bucket_fhs[b].write(sorted_chunk[lo:hi].tobytes())
                    bucket_counts[b] += hi - lo

            written += size
            progress('Pass 1/2', written * RECORD_SIZE, total_bytes, t0)

    elapsed = time.time() - t0
    print(f'\r  Pass 1/2 done: {n_records:,} records scattered to {N} buckets'
          f' in {fmt_time(elapsed)}' + ' ' * 20)
    return bucket_counts


# â”€â”€ Pass 2: gather â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def gather_pass(bucket_paths, bucket_counts, output_path, meta, rng):
    """
    Load each bucket into RAM, shuffle in-place, append to output sequentially.
    """
    n_records = int(bucket_counts.sum())
    total_bytes = n_records * RECORD_SIZE
    written = 0
    t0 = time.time()

    with open(output_path, 'wb') as out:
        _write_header(out, n_records, meta['input_size'])

        for i, (bp, count) in enumerate(zip(bucket_paths, bucket_counts)):
            if count == 0:
                continue
            # Load entire bucket into RAM
            buf = np.empty(count, dtype=RECORD_DTYPE)
            with open(str(bp), 'rb') as f:
                f.readinto(buf)
            # In-place Fisher-Yates shuffle
            rng.shuffle(buf)
            # Append to output
            out.write(buf.tobytes())
            written += count
            progress('Pass 2/2', written * RECORD_SIZE, total_bytes, t0,
                     suffix=f'  bucket {i+1}/{len(bucket_paths)}')

    elapsed = time.time() - t0
    print(f'\r  Pass 2/2 done: {n_records:,} records written'
          f' in {fmt_time(elapsed)}' + ' ' * 20)
    return n_records


# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('input',           help='Input .bin file')
    p.add_argument('output', nargs='?', default=None,
                   help='Output .bin file (default: <input>_reshuffled.bin)')
    p.add_argument('--inplace',       action='store_true',
                   help='Replace input with output after verification')
    p.add_argument('--n-buckets',     type=int, default=0,
                   help='Scatter buckets (0=auto: targets ~2 GB/bucket)')
    p.add_argument('--seed',          type=int, default=1337,
                   help='RNG seed (default 1337)')
    p.add_argument('--temp-dir',      default=None,
                   help='Directory for temp bucket files (default: output directory)')
    p.add_argument('--read-chunk',    type=int, default=2_000_000,
                   help='Records per read in scatter pass (default 2M â‰ˆ 272 MB)')
    args = p.parse_args()

    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f'ERROR: not found: {input_path}'); sys.exit(1)

    # â”€â”€ Read + validate input header â”€â”€
    meta = _read_header(str(input_path))
    n    = meta['n_records']
    file_size = input_path.stat().st_size
    expected  = HEADER_SIZE + n * RECORD_SIZE
    if file_size != expected:
        print(f'ERROR: file size {file_size:,} != expected {expected:,}'); sys.exit(1)

    # â”€â”€ Paths â”€â”€
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = input_path.with_name(input_path.stem + '_reshuffled.bin')

    temp_dir = Path(args.temp_dir) if args.temp_dir else output_path.parent
    temp_dir.mkdir(parents=True, exist_ok=True)

    # â”€â”€ Disk space check â”€â”€
    free = shutil.disk_usage(temp_dir).free
    needed = file_size * 2  # temp buckets (= file_size) + output (= file_size)
    print(f'Input:    {input_path}')
    print(f'          {n:,} records  ({fmt_gb(file_size)})')
    print(f'Output:   {output_path}')
    print(f'Temp dir: {temp_dir}')
    print(f'Disk:     {fmt_gb(free)} free  /  ~{fmt_gb(needed)} needed (temp + output)')
    if free < needed:
        print(f'WARNING: Low disk space â€” {fmt_gb(free)} free, need ~{fmt_gb(needed)}')
        ans = input('Continue anyway? [y/N] ').strip().lower()
        if ans != 'y':
            sys.exit(0)

    # â”€â”€ Bucket count â”€â”€
    if args.n_buckets > 0:
        n_buckets = args.n_buckets
    else:
        target_bucket_bytes = 2 * 1024**3  # 2 GB per bucket
        n_buckets = max(8, min(256, int(file_size / target_bucket_bytes) + 1))

    bucket_bytes = file_size / n_buckets
    print(f'Buckets:  {n_buckets}  (~{fmt_gb(bucket_bytes)} each)')
    print(f'Seed:     {args.seed}')
    print()

    rng = np.random.default_rng(args.seed)

    # â”€â”€ Create temp bucket files â”€â”€
    bucket_paths = [
        temp_dir / f'_nnue_bucket_{args.seed}_{i:04d}.tmp'
        for i in range(n_buckets)
    ]
    bucket_fhs = []
    t_wall = time.time()

    try:
        bucket_fhs = [open(str(bp), 'wb') for bp in bucket_paths]

        # Pass 1
        bucket_counts = scatter_pass(
            str(input_path), bucket_fhs, n, rng, args.read_chunk
        )

        # Close all bucket handles before reading them back
        for fh in bucket_fhs:
            fh.close()
        bucket_fhs = []

        print(f'  Bucket sizes: min={bucket_counts.min():,}  '
              f'max={bucket_counts.max():,}  '
              f'mean={bucket_counts.mean():,.0f}  '
              f'(expected ~{n // n_buckets:,})\n')

        # Pass 2
        out_records = gather_pass(
            bucket_paths, bucket_counts, str(output_path), meta, rng
        )

    except KeyboardInterrupt:
        print('\nInterrupted.')
        sys.exit(1)
    except Exception as e:
        print(f'\nERROR: {e}')
        raise
    finally:
        for fh in bucket_fhs:
            try: fh.close()
            except: pass
        for bp in bucket_paths:
            try:
                if bp.exists(): bp.unlink()
            except: pass

    # â”€â”€ Verify output â”€â”€
    print(f'\nVerifying output...')
    out_meta = _read_header(str(output_path))
    out_size = output_path.stat().st_size
    ok = True
    if out_meta['n_records'] != n:
        print(f'  ERROR: record count {out_meta["n_records"]:,} != {n:,}'); ok = False
    else:
        print(f'  Records:   {out_meta["n_records"]:,}  OK')
    if out_size != file_size:
        print(f'  ERROR: size {out_size:,} != {file_size:,}'); ok = False
    else:
        print(f'  File size: {fmt_gb(out_size)}  OK')

    if not ok:
        print('Verification FAILED. Output may be corrupt.'); sys.exit(1)

    total = time.time() - t_wall
    print(f'\nTotal time: {fmt_time(total)}'
          f'  ({fmt_rate(file_size * 2 / total)} effective throughput)')

    if args.inplace and ok:
        print(f'\nReplacing input with shuffled output...')
        input_path.unlink()
        output_path.rename(input_path)
        print(f'  Done â†’ {input_path}')
        print(f'\nNext: python tools/validate_bin.py {input_path}')
    else:
        print(f'\nOutput â†’ {output_path}')
        print(f'\nNext steps:')
        print(f'  python tools/validate_bin.py {output_path}')
        print(f'  # if OK, replace original:')
        print(f'  # move "{output_path.name}" "{input_path.name}"')


if __name__ == '__main__':
    main()
