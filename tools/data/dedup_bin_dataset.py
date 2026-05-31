"""
dedup_bin_dataset.py â€” Deduplicate NNUE binary datasets by exact position signature.

This tool builds a refined .bin dataset where duplicate positions share one record
with mean score/WDL labels.

Dedup key (exact signature):
  - stm
  - bucket
  - n_white / n_black
  - white feature indices
  - black feature indices

This is the exact information stored in the training binaries apart from the labels,
so duplicates are merged only when the NNUE training input is identical.

Output:
  - deduplicated .bin file in the same 136-byte record format
  - JSON manifest summarizing input/output counts and duplicate reduction

Usage:
  python tools/dedup_bin_dataset.py --output data/processed/all_dedup.bin
  python tools/dedup_bin_dataset.py --year-min 2024 --output data/processed/recent_dedup.bin
  python tools/dedup_bin_dataset.py --limit-files 4 --output data/processed/smoke.bin

Notes:
  - The full dataset is large; expect a long run and substantial temporary disk usage.
  - Temporary shard files are removed on success unless --keep-temp is used.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.data import RECORD_DTYPE, HEADER_SIZE, BinaryWriter, _read_header

KEY_DTYPE = np.dtype([
    ('stm', 'u1'),
    ('bucket', 'u1'),
    ('n_white', 'u1'),
    ('n_black', 'u1'),
    ('white_feats', '<u2', (32,)),
    ('black_feats', '<u2', (32,)),
])

PRIMES_W = np.array([
    3, 5, 7, 11, 13, 17, 19, 23,
    29, 31, 37, 41, 43, 47, 53, 59,
    61, 67, 71, 73, 79, 83, 89, 97,
    101, 103, 107, 109, 113, 127, 131, 137,
], dtype=np.uint32)
PRIMES_B = np.array([
    139, 149, 151, 157, 163, 167, 173, 179,
    181, 191, 193, 197, 199, 211, 223, 227,
    229, 233, 239, 241, 251, 257, 263, 269,
    271, 277, 281, 283, 293, 307, 311, 313,
], dtype=np.uint32)

COUNT_BUCKETS = [1, 2, 5, 10, 50, 100, 1000]


def _iter_input_bins(year_min: int | None, year_max: int | None, limit_files: int,
                     extra_files: list[str] | None = None) -> list[str]:
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
        if limit_files > 0 and len(out) >= limit_files:
            break
    # Append supplemental files (e.g. endgames.bin) that don't match the q*.bin naming pattern.
    for extra in (extra_files or []):
        extra = os.path.abspath(extra)
        if not os.path.isfile(extra):
            raise SystemExit(f'--extra-files: file not found: {extra}')
        if os.path.getsize(extra) <= 32:
            print(f'[warn] --extra-files: skipping empty file {extra}')
            continue
        if extra not in out:
            out.append(extra)
    return out


def _shard_ids(chunk: np.ndarray, num_shards: int) -> np.ndarray:
    w = chunk['white_feats'].astype(np.uint32)
    b = chunk['black_feats'].astype(np.uint32)

    h = np.uint32(chunk['stm'].astype(np.uint32) * np.uint32(0x9E3779B1))
    h ^= np.uint32(chunk['bucket'].astype(np.uint32) * np.uint32(0x85EBCA77))
    h ^= np.uint32(chunk['n_white'].astype(np.uint32) * np.uint32(0xC2B2AE3D))
    h ^= np.uint32(chunk['n_black'].astype(np.uint32) * np.uint32(0x27D4EB2F))
    h ^= np.bitwise_xor.reduce(w * PRIMES_W[None, :], axis=1)
    h ^= np.bitwise_xor.reduce(b * PRIMES_B[None, :], axis=1)
    return (h % np.uint32(num_shards)).astype(np.int32)


def _copy_keys(arr: np.ndarray) -> np.ndarray:
    keys = np.empty(len(arr), dtype=KEY_DTYPE)
    keys['stm'] = arr['stm']
    keys['bucket'] = arr['bucket']
    keys['n_white'] = arr['n_white']
    keys['n_black'] = arr['n_black']
    keys['white_feats'] = arr['white_feats']
    keys['black_feats'] = arr['black_feats']
    return keys


def _bucket_name(count: int) -> str:
    if count <= 1:
        return '1'
    if count == 2:
        return '2'
    if count <= 5:
        return '3-5'
    if count <= 10:
        return '6-10'
    if count <= 50:
        return '11-50'
    if count <= 100:
        return '51-100'
    if count <= 1000:
        return '101-1000'
    return '1001+'


def _pass1_shard(inputs: list[str], shard_dir: Path, num_shards: int, chunk_records: int) -> tuple[int, int]:
    shard_dir.mkdir(parents=True, exist_ok=True)
    handles = [open(shard_dir / f'shard_{i:04d}.binraw', 'wb') for i in range(num_shards)]
    total_records = 0
    total_files = 0
    try:
        for file_idx, path in enumerate(inputs, 1):
            meta = _read_header(path)
            n = meta['n_records']
            arr = np.memmap(path, dtype=RECORD_DTYPE, mode='r', offset=HEADER_SIZE, shape=(n,))
            total_files += 1
            total_records += n
            print(f"[pass1] {file_idx}/{len(inputs)} {os.path.basename(path)}  records={n:,}")
            for start in range(0, n, chunk_records):
                end = min(start + chunk_records, n)
                chunk = np.array(arr[start:end], copy=True)
                ids = _shard_ids(chunk, num_shards)
                order = np.argsort(ids, kind='stable')
                chunk = chunk[order]
                ids = ids[order]
                boundaries = np.flatnonzero(np.diff(ids)) + 1
                starts = np.concatenate(([0], boundaries))
                ends = np.concatenate((boundaries, [len(ids)]))
                for lo, hi in zip(starts, ends):
                    shard = int(ids[lo])
                    handles[shard].write(chunk[lo:hi].tobytes())
    finally:
        for fh in handles:
            fh.close()
    return total_files, total_records


def _pass2_reduce(shard_dir: Path, output_path: Path, manifest_path: Path, keep_temp: bool) -> dict:
    shard_paths = sorted(shard_dir.glob('shard_*.binraw'))
    total_input = 0
    total_output = 0
    dup_hist: dict[str, int] = {}
    max_dup = 0
    t0 = time.time()

    with BinaryWriter(str(output_path)) as writer:
        for idx, shard_path in enumerate(shard_paths, 1):
            if shard_path.stat().st_size == 0:
                if not keep_temp:
                    shard_path.unlink(missing_ok=True)
                continue

            arr = np.fromfile(shard_path, dtype=RECORD_DTYPE)
            total_input += len(arr)
            print(f"[pass2] {idx}/{len(shard_paths)} {shard_path.name}  records={len(arr):,}")

            keys = _copy_keys(arr)
            key_view = keys.view(np.dtype((np.void, KEY_DTYPE.itemsize))).reshape(-1)
            _, first_idx, inverse, counts = np.unique(
                key_view,
                return_index=True,
                return_inverse=True,
                return_counts=True,
            )

            uniq = arr[first_idx].copy()
            score = arr['score'].astype(np.float64)
            wdl = arr['wdl'].astype(np.float64)
            sum_score = np.bincount(inverse, weights=score)
            sum_wdl = np.bincount(inverse, weights=wdl)

            uniq['score'] = np.rint(sum_score / counts).astype(np.int16)
            uniq['wdl'] = (sum_wdl / counts).astype(np.float16)
            writer.write_batch(uniq)
            total_output += len(uniq)

            for c in counts.tolist():
                dup_hist[_bucket_name(int(c))] = dup_hist.get(_bucket_name(int(c)), 0) + 1
            max_dup = max(max_dup, int(counts.max()))

            if not keep_temp:
                shard_path.unlink(missing_ok=True)

    elapsed = time.time() - t0
    manifest = {
        'output_path': str(output_path),
        'input_records': int(total_input),
        'output_records': int(total_output),
        'duplicates_removed': int(total_input - total_output),
        'reduction_fraction': float((total_input - total_output) / max(total_input, 1)),
        'max_duplicate_count': int(max_dup),
        'duplicate_count_histogram': dup_hist,
        'elapsed_s_pass2': round(elapsed, 1),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description='Deduplicate NNUE binary datasets by exact position signature')
    ap.add_argument('--output', required=True, help='Output deduplicated .bin path')
    ap.add_argument('--year-min', type=int, default=None, help='Only include bins from this year onward')
    ap.add_argument('--year-max', type=int, default=None, help='Only include bins up to this year')
    ap.add_argument('--limit-files', type=int, default=0, help='Only include the first N files after filtering (smoke test)')
    ap.add_argument('--num-shards', type=int, default=256, help='Temporary shard count (default: 256)')
    ap.add_argument('--chunk-records', type=int, default=1_000_000, help='Records per routing chunk (default: 1,000,000)')
    ap.add_argument('--temp-dir', default=None, help='Temporary shard directory (default: sibling of output)')
    ap.add_argument('--keep-temp', action='store_true', help='Keep temporary shard files after completion')
    ap.add_argument('--extra-files', nargs='+', default=[], metavar='PATH',
                    help='Additional .bin files to include (e.g. endgames.bin). Appended after the monthly files.')
    args = ap.parse_args()

    inputs = _iter_input_bins(args.year_min, args.year_max, args.limit_files, args.extra_files)
    if not inputs:
        raise SystemExit('No input .bin files matched the requested filters.')

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(args.temp_dir) if args.temp_dir else output_path.parent / (output_path.stem + '_tmp_shards')
    manifest_path = output_path.with_suffix(output_path.suffix + '.manifest.json')

    total_bytes = sum(os.path.getsize(p) for p in inputs)
    total_records = sum(_read_header(p)['n_records'] for p in inputs)
    print('Dedup input set:')
    print(f"  files        : {len(inputs)}")
    print(f"  records      : {total_records:,}")
    print(f"  bytes        : {total_bytes / 1e9:.1f} GB")
    print(f"  output       : {output_path}")
    print(f"  temp dir     : {temp_dir}")
    print(f"  shard count  : {args.num_shards}")
    print(f"  chunk records: {args.chunk_records:,}")
    print()

    t0 = time.time()
    _pass1_shard(inputs, temp_dir, args.num_shards, args.chunk_records)
    pass1_s = time.time() - t0
    print(f"\n[pass1] complete in {pass1_s/60:.1f} min\n")

    manifest = _pass2_reduce(temp_dir, output_path, manifest_path, args.keep_temp)
    manifest['elapsed_s_total'] = round(time.time() - t0, 1)
    manifest['input_files'] = len(inputs)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')

    print('\nDone.')
    print(f"  output             : {output_path}")
    print(f"  manifest           : {manifest_path}")
    print(f"  input records      : {manifest['input_records']:,}")
    print(f"  output records     : {manifest['output_records']:,}")
    print(f"  duplicates removed : {manifest['duplicates_removed']:,}")
    print(f"  reduction          : {100.0 * manifest['reduction_fraction']:.2f}%")
    print(f"  max duplicate ct   : {manifest['max_duplicate_count']:,}")


if __name__ == '__main__':
    main()
