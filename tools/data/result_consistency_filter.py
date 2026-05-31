"""
result_consistency_filter.py â€” Drop records where engine eval and game-result
WDL strongly disagree.

Useful only AFTER the dataset has been re-prepped with --result-wdl-blend > 0.
For datasets prepped with blend=0.0 (where stored wdl == sigmoid(score/600))
this filter is a no-op.

A record is dropped when:
  * |score| > eval_threshold  (e.g. 500 cp = engine confident)
  AND
  * sign(score) disagrees with sign(stored_wdl - 0.5) by at least
    `wdl_disagreement_threshold` (e.g. 0.4 â†’ wdl off by 40 percentage points)

Run AFTER the binary has been shuffled (operates streaming, position-shuffled
output preserved).

Usage:
    python tools/result_consistency_filter.py <bin> [--inplace]
                                                 [--eval-threshold 500]
                                                 [--wdl-disagreement 0.4]
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from ml.data import (
    RECORD_DTYPE, RECORD_SIZE, HEADER_SIZE, HEADER_MAGIC, _read_header,
)
from ml.arch import INPUT_SIZE


def main():
    p = argparse.ArgumentParser()
    p.add_argument("input")
    p.add_argument("--output", default=None,
                   help="Output path (default: <input>.consistency_filtered.bin)")
    p.add_argument("--inplace", action="store_true",
                   help="Replace input with filtered output after success")
    p.add_argument("--eval-threshold", type=int, default=500,
                   help="|score| above which engine is considered confident (default 500)")
    p.add_argument("--wdl-disagreement", type=float, default=0.4,
                   help="WDL disagreement magnitude required to drop "
                        "(default 0.4 = 40 percentage points)")
    p.add_argument("--read-chunk", type=int, default=2_000_000)
    args = p.parse_args()

    in_path = Path(args.input).resolve()
    out_path = Path(args.output) if args.output else in_path.with_name(in_path.stem + ".consistency_filtered.bin")

    meta = _read_header(str(in_path))
    n = meta['n_records']
    print(f"Input:  {in_path}  ({n:,} records, {n*RECORD_SIZE/1024**3:.1f} GB payload)")
    print(f"Output: {out_path}")
    print(f"Filter: drop if |score|>{args.eval_threshold} AND "
          f"sign(score) disagrees with stored wdl by >= {args.wdl_disagreement}")
    print()

    # First detect whether stored wdl is just sigmoid(score/600) (blend=0).
    # Sample first 200K records.
    with open(in_path, 'rb') as f:
        f.seek(HEADER_SIZE)
        sample = np.empty(min(200_000, n), dtype=RECORD_DTYPE)
        f.readinto(sample)
    s = sample['score'].astype(np.float32)
    w = sample['wdl'].astype(np.float32)
    derived = 1.0 / (1.0 + np.exp(-s / 600.0))
    diff = float(np.abs(w - derived).mean())
    print(f"Sample diff(stored_wdl, sigmoid(score/600)): mean={diff:.4f}")
    if diff < 0.01:
        print("  WARNING: stored WDL appears to be pure sigmoid(score/600) "
              "(no game-result blend). This filter will be a no-op. Aborting.")
        sys.exit(0)

    kept = 0
    dropped = 0
    pos = 0
    t0 = time.time()

    with open(in_path, 'rb') as fin, open(out_path, 'wb') as fout:
        fout.write(b'\x00' * HEADER_SIZE)
        fin.seek(HEADER_SIZE)
        while pos < n:
            size = min(args.read_chunk, n - pos)
            buf = np.empty(size, dtype=RECORD_DTYPE)
            fin.readinto(buf)
            sc = buf['score'].astype(np.float32)
            wd = buf['wdl'].astype(np.float32)
            # Engine confidence
            eng_winning = sc > args.eval_threshold
            eng_losing = sc < -args.eval_threshold
            # Result-derived signal: wdl is from white's POV; >0.5 = white winning
            result_winning = wd > (0.5 + args.wdl_disagreement / 2)
            result_losing = wd < (0.5 - args.wdl_disagreement / 2)
            # Disagreement: engine says winning but result says losing (or vice versa)
            drop = (eng_winning & result_losing) | (eng_losing & result_winning)
            keep = ~drop
            kh = int(keep.sum())
            if kh:
                fout.write(buf[keep].tobytes())
            kept += kh
            dropped += size - kh
            pos += size
            elapsed = time.time() - t0
            rate = (pos * RECORD_SIZE) / elapsed / 1024**3
            print(f"\r  {pos/n*100:.1f}%  kept={kept:,}  dropped={dropped:,}  "
                  f"{rate:.2f} GB/s  ", end='', flush=True)
    print()

    with open(out_path, 'r+b') as f:
        f.seek(0)
        f.write(struct.pack('<8sIII12x', HEADER_MAGIC, 1, kept, INPUT_SIZE))

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s")
    print(f"  Kept:    {kept:,}  ({kept/n*100:.1f}%)")
    print(f"  Dropped: {dropped:,}  ({dropped/n*100:.1f}%)")

    if args.inplace:
        os.replace(out_path, in_path)
        print(f"Replaced input: {in_path}")


if __name__ == "__main__":
    main()
