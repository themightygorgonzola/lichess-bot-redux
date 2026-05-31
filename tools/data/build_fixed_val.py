"""
build_fixed_val.py -- Build a stable fixed validation set from monthly training files.

For each monthly .bin file, extracts the natural val portion (last 10% of records,
matching the sequential split used by MultiChunkedBinaryLoader with val_frac=0.1),
then takes a random sample of up to --sample-per-file records.

The result is a single fixed val.bin that is:
  - Identical every run (same records, same order after shuffle with fixed seed)
  - Drawn from the same positions the training loader EXCLUDES (no contamination)
  - Balanced across months (proportional, not dominated by large months)

Usage:
    cd lichess-bot-redux
    python tools/data/build_fixed_val.py
    python tools/data/build_fixed_val.py --sample-per-file 8000 --seed 99
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.data import RECORD_DTYPE, HEADER_SIZE, BinaryWriter, _read_header  # type: ignore[import]

RECORD_SIZE = RECORD_DTYPE.itemsize   # 136
VAL_FRAC    = 0.1


def main() -> None:
    ap = argparse.ArgumentParser(description='Build stable fixed validation set')
    ap.add_argument('--train-glob',        default='data/training/q*.bin')
    ap.add_argument('--pipeline-state',    default='data/pipeline_state.json')
    ap.add_argument('--output',            default='data/val/val-fixed.bin')
    ap.add_argument('--sample-per-file',   type=int, default=5_000,
                    help='Max records to sample from each monthly file (default: 5000)')
    ap.add_argument('--seed',              type=int, default=42)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)

    # ── Resolve file list ─────────────────────────────────────────────────────
    import glob as _glob
    paths = sorted(_glob.glob(str(ROOT / args.train_glob)))
    if not paths:
        raise SystemExit(f'No files matched: {args.train_glob}')

    # Filter by pipeline_state 'done' if the state file exists
    state_path = ROOT / args.pipeline_state
    if state_path.exists():
        with open(state_path) as f:
            state = json.load(f)
        done_months = {
            k for k, v in state.get('months', {}).items()
            if v.get('state') == 'done'
        }
        def _month_key(p: str) -> str:
            m = re.search(r'q(\d{4})_(\d{2})\.bin$', p)
            return f'{m.group(1)}-{m.group(2)}' if m else ''
        before = len(paths)
        paths = [p for p in paths if _month_key(p) in done_months]
        print(f'Pipeline state filter: {before} -> {len(paths)} done months')

    print(f'Building val set from {len(paths)} files  (sample_per_file={args.sample_per_file:,})')

    # ── Sample from each file's val portion ───────────────────────────────────
    t0       = time.time()
    all_recs: list[np.ndarray] = []
    total_available = 0
    total_sampled   = 0

    for i, path in enumerate(paths):
        meta    = _read_header(path)
        n_total = meta['n_records']
        if n_total == 0:
            continue

        n_val   = max(1, int(n_total * VAL_FRAC))
        val_start = n_total - n_val          # first record index of the val portion

        # Memory-map only the val portion
        arr = np.memmap(path, dtype=RECORD_DTYPE, mode='r',
                        offset=HEADER_SIZE + val_start * RECORD_SIZE,
                        shape=(n_val,))

        total_available += n_val
        n_take = min(n_val, args.sample_per_file)
        idx    = rng.choice(n_val, size=n_take, replace=False)
        idx.sort()   # sequential access is faster on mmap
        sample = np.array(arr[idx], copy=True)  # copy out of mmap
        all_recs.append(sample)
        total_sampled += n_take
        del arr   # release mmap immediately

        if (i + 1) % 10 == 0 or (i + 1) == len(paths):
            print(f'  {i+1}/{len(paths)} files  sampled so far: {total_sampled:,}  '
                  f'({time.time()-t0:.0f}s)', end='\r')

    print()

    if not all_recs:
        raise SystemExit('No records collected -- check file paths and pipeline state')

    # ── Concatenate, shuffle, write ───────────────────────────────────────────
    combined = np.concatenate(all_recs)
    rng.shuffle(combined)

    out_path = ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with BinaryWriter(str(out_path)) as writer:
        writer.write_batch(combined)

    elapsed = time.time() - t0
    size_mb = out_path.stat().st_size / 1e6

    print(f'\nDone in {elapsed:.0f}s')
    print(f'  Source files:   {len(paths)}')
    print(f'  Total available val records: {total_available:,}')
    print(f'  Records written: {len(combined):,}')
    print(f'  Output: {out_path}  ({size_mb:.1f} MB)')

    # ── Bucket distribution ───────────────────────────────────────────────────
    print('\nBucket distribution:')
    for b in range(8):
        n = int((combined['bucket'] == b).sum())
        pct = 100.0 * n / len(combined)
        print(f'  bucket {b}: {n:,}  ({pct:.1f}%)')


if __name__ == '__main__':
    main()
