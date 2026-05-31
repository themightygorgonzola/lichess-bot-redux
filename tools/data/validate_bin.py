#!/usr/bin/env python3
"""
tools/validate_bin.py â€” Comprehensive validation of NNUE .bin training dataset.

Checks:
  1. Header integrity      magic, version, INPUT_SIZE match
  2. File size             HEADER_SIZE + n_records * RECORD_SIZE
  3. Field validity        stm, bucket, n_feats, feature indices, wdl (sampled)
  4. Score uniformity      per-chunk std and high-score% across the file
                           â€” the key metric for shuffle quality
  5. Balance              stm split, bucket distribution, overall score stats

Uniformity verdict:
  std ratio = max_chunk_std / min_chunk_std
    < 1.3  â†’  GOOD   (well-shuffled, training will converge smoothly)
    1.3-2  â†’  WARN   (some variation, monitor val loss oscillation)
    > 2.0  â†’  BAD    (run tools/reshuffle_bin.py before training)

Usage:
  python tools/validate_bin.py data/processed/mean-alltime-dedup-shuffled.bin
  python tools/validate_bin.py path/to/file.bin --n-samples 100
"""

import sys
import os
import time
import argparse
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml.data import _read_header, RECORD_DTYPE, RECORD_SIZE, HEADER_SIZE
from ml.arch import INPUT_SIZE

MAX_FEATS  = 32
MAX_BUCKET = 7


def fmt_gb(n):
    return f'{n / 1024**3:.1f} GB'


def read_chunk(path, record_start, size):
    buf = np.empty(size, dtype=RECORD_DTYPE)
    with open(path, 'rb') as f:
        f.seek(HEADER_SIZE + record_start * RECORD_SIZE)
        f.readinto(buf)
    return buf


def check_fields(buf, label):
    """Return list of (field, error_count, description) tuples for any violations."""
    issues = []
    n = len(buf)

    bad = int(np.count_nonzero((buf['stm'] != 0) & (buf['stm'] != 1)))
    if bad: issues.append(('stm', bad, f'{bad}/{n} records not in {{0,1}}'))

    bad = int(np.count_nonzero(buf['bucket'] > MAX_BUCKET))
    if bad: issues.append(('bucket', bad, f'{bad}/{n} records with bucket > {MAX_BUCKET}'))

    bad = int(np.count_nonzero((buf['n_white'] == 0) | (buf['n_white'] > MAX_FEATS)))
    if bad: issues.append(('n_white', bad, f'{bad}/{n} records out of range [1, {MAX_FEATS}]'))

    bad = int(np.count_nonzero((buf['n_black'] == 0) | (buf['n_black'] > MAX_FEATS)))
    if bad: issues.append(('n_black', bad, f'{bad}/{n} records out of range [1, {MAX_FEATS}]'))

    max_wf = int(buf['white_feats'].max())
    max_bf = int(buf['black_feats'].max())
    if max_wf >= INPUT_SIZE:
        issues.append(('white_feats', 1, f'max index {max_wf} >= INPUT_SIZE {INPUT_SIZE}'))
    if max_bf >= INPUT_SIZE:
        issues.append(('black_feats', 1, f'max index {max_bf} >= INPUT_SIZE {INPUT_SIZE}'))

    wdl = buf['wdl'].astype(np.float32)
    bad = int(np.count_nonzero(~np.isfinite(wdl) | (wdl < 0) | (wdl > 1)))
    if bad: issues.append(('wdl', bad, f'{bad}/{n} records not finite or not in [0, 1]'))

    return issues


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('path',          help='Path to .bin file to validate')
    p.add_argument('--n-samples',   type=int, default=60,
                   help='Sample points across the file for distribution check (default 60)')
    p.add_argument('--sample-size', type=int, default=200_000,
                   help='Records per sample point (default 200,000)')
    args = p.parse_args()

    path = str(Path(args.path).resolve())
    if not os.path.exists(path):
        print(f'ERROR: File not found: {path}'); sys.exit(1)

    errors   = []
    warnings = []

    print(f'Validating: {path}')
    print('=' * 70)

    # â”€â”€ 1. Header â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\n[1] Header')
    try:
        meta = _read_header(path)
        n    = meta['n_records']
        print(f'    Magic:      NNUE_BIN  OK')
        print(f'    Version:    {meta["version"]}')
        print(f'    Records:    {n:,}')
        print(f'    Input size: {meta["input_size"]}  (arch expects {INPUT_SIZE})')
        if meta['input_size'] != INPUT_SIZE:
            errors.append(f'INPUT_SIZE mismatch: file={meta["input_size"]}, arch={INPUT_SIZE}')
            print('    !!! MISMATCH â€” features were prepped for a different arch')
    except Exception as e:
        print(f'    FAILED: {e}')
        sys.exit(1)

    # â”€â”€ 2. File size â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\n[2] File size')
    actual   = os.path.getsize(path)
    expected = HEADER_SIZE + n * RECORD_SIZE
    print(f'    Actual:   {fmt_gb(actual)}  ({actual:,} bytes)')
    print(f'    Expected: {fmt_gb(expected)}  ({expected:,} bytes)')
    if actual != expected:
        errors.append(f'File size mismatch: {actual:,} != {expected:,}')
        print(f'    !!! SIZE MISMATCH â€” file may be truncated or corrupt')
    else:
        print(f'    OK')

    # â”€â”€ 3. Field validity + score distribution (sampled) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    n_samples   = args.n_samples
    sample_size = min(args.sample_size, n)
    max_start   = max(0, n - sample_size)
    sample_starts = np.linspace(0, max_start, n_samples, dtype=np.int64)

    print(f'\n[3] Field validity ({n_samples} samples Ã— {sample_size:,} records'
          f' = {n_samples * sample_size / 1e6:.0f}M records sampled)')

    chunk_stds    = []
    chunk_means   = []
    chunk_hi_pcts = []   # |score| > 1200 cp
    all_issues    = []
    t0 = time.time()

    for i, start in enumerate(sample_starts):
        buf = read_chunk(path, int(start), sample_size)
        issues = check_fields(buf, f'sample {i}')
        all_issues.extend(issues)

        scores = buf['score'].astype(np.float32)
        chunk_stds.append(float(scores.std()))
        chunk_means.append(float(scores.mean()))
        chunk_hi_pcts.append(float((np.abs(scores) > 1200).mean() * 100))

        if (i + 1) % 10 == 0 or i == args.n_samples - 1:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (n_samples - i - 1)
            print(f'\r    {i+1}/{n_samples} samples...  ETA {eta:.0f}s   ',
                  end='', flush=True)

    elapsed = time.time() - t0
    print(f'\r    {n_samples} samples scanned in {elapsed:.1f}s' + ' ' * 30)

    if all_issues:
        # Deduplicate by field name
        seen = {}
        for field, count, desc in all_issues:
            if field not in seen:
                seen[field] = (count, desc)
            else:
                seen[field] = (seen[field][0] + count, desc)
        for field, (count, desc) in seen.items():
            errors.append(f'Field {field}: {desc}')
            print(f'    ERROR â€” {field}: {desc}')
    else:
        print(f'    All fields valid across {n_samples * sample_size:,} sampled records  OK')

    # â”€â”€ 4. Score distribution uniformity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(f'\n[4] Score distribution uniformity across file')

    chunk_stds    = np.array(chunk_stds)
    chunk_means   = np.array(chunk_means)
    chunk_hi_pcts = np.array(chunk_hi_pcts)

    std_min, std_max = chunk_stds.min(), chunk_stds.max()
    std_ratio   = std_max / max(std_min, 1.0)
    mean_range  = chunk_means.max() - chunk_means.min()
    hi_min, hi_max = chunk_hi_pcts.min(), chunk_hi_pcts.max()
    hi_ratio    = hi_max / max(hi_min, 0.01)

    print(f'    Score std:     min={std_min:.0f}  max={std_max:.0f}  '
          f'ratio={std_ratio:.2f}Ã—')
    print(f'    Score mean:    min={chunk_means.min():+.0f}  max={chunk_means.max():+.0f}  '
          f'range={mean_range:.0f} cp')
    print(f'    |score|>1200:  min={hi_min:.1f}%  max={hi_max:.1f}%  '
          f'ratio={hi_ratio:.2f}Ã—')

    # Find worst sample points
    worst_std_idx  = int(np.argmax(chunk_stds))
    best_std_idx   = int(np.argmin(chunk_stds))
    worst_pos_pct  = sample_starts[worst_std_idx] / n * 100
    best_pos_pct   = sample_starts[best_std_idx]  / n * 100
    print(f'    Worst chunk:   {chunk_stds[worst_std_idx]:.0f} std  '
          f'{chunk_hi_pcts[worst_std_idx]:.1f}% high-score  '
          f'@ {worst_pos_pct:.0f}% into file')
    print(f'    Best chunk:    {chunk_stds[best_std_idx]:.0f} std  '
          f'{chunk_hi_pcts[best_std_idx]:.1f}% high-score  '
          f'@ {best_pos_pct:.0f}% into file')

    # Verdict
    if std_ratio > 2.0:
        warnings.append(
            f'Score std ratio {std_ratio:.2f}Ã— (threshold 2.0) â€” '
            f'dataset is NOT uniformly shuffled at position level. '
            f'Run: python tools/reshuffle_bin.py {path} --inplace'
        )
        print(f'    Verdict:       BAD  (ratio {std_ratio:.2f}Ã— > 2.0) â€” reshuffle needed')
    elif std_ratio > 1.3:
        warnings.append(
            f'Score std ratio {std_ratio:.2f}Ã— (threshold 1.3) â€” '
            f'moderate chunk variation. Consider reshuffling.'
        )
        print(f'    Verdict:       WARN (ratio {std_ratio:.2f}Ã— in 1.3â€“2.0 range)')
    else:
        print(f'    Verdict:       GOOD (ratio {std_ratio:.2f}Ã— < 1.3)')

    # ASCII sparkline of std across file
    buckets_bar = 40
    normalized  = (chunk_stds - chunk_stds.min()) / max(chunk_stds.max() - chunk_stds.min(), 1)
    bar_chars   = 'â–â–‚â–ƒâ–„â–…â–†â–‡â–ˆ'
    spark = ''.join(bar_chars[min(7, int(v * 8))] for v in
                    normalized[np.linspace(0, len(normalized)-1, buckets_bar, dtype=int)])
    print(f'    Std profile:   |{spark}|  (left=file start, right=end)')

    # â”€â”€ 5. Balance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(f'\n[5] Overall balance (from all sample records)')

    all_stm  = []
    all_buck = []
    all_sc   = []
    # Re-read first and last few samples for a combined view
    for start in sample_starts[::max(1, n_samples // 10)]:
        buf = read_chunk(path, int(start), sample_size)
        all_stm.append(buf['stm'])
        all_buck.append(buf['bucket'])
        all_sc.append(buf['score'].astype(np.float32))

    all_stm  = np.concatenate(all_stm)
    all_buck = np.concatenate(all_buck)
    all_sc   = np.concatenate(all_sc)

    stm_black_pct = all_stm.mean() * 100
    print(f'    STM balance:  {100 - stm_black_pct:.1f}% white / {stm_black_pct:.1f}% black')
    if abs(stm_black_pct - 50) > 10:
        warnings.append(f'STM imbalance: {stm_black_pct:.1f}% black-to-move (expected ~50%)')

    bvals, bcounts = np.unique(all_buck, return_counts=True)
    bpcts = bcounts / len(all_buck) * 100
    print(f'    Bucket dist:  ' + '  '.join(f'b{b}={pct:.0f}%' for b, pct in zip(bvals, bpcts)))

    print(f'    Score:        mean={all_sc.mean():+.0f} cp  std={all_sc.std():.0f} cp  '
          f'median={np.median(all_sc):+.0f} cp')
    print(f'                  |s|>800: {(np.abs(all_sc)>800).mean()*100:.1f}%  '
          f'|s|>1200: {(np.abs(all_sc)>1200).mean()*100:.1f}%  '
          f'|s|>2000: {(np.abs(all_sc)>2000).mean()*100:.1f}%')

    # â”€â”€ Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print('\n' + '=' * 70)
    if errors:
        print(f'RESULT: FAILED  ({len(errors)} error(s), {len(warnings)} warning(s))')
        for e in errors:
            print(f'  ERROR: {e}')
    elif warnings:
        print(f'RESULT: WARNINGS  (0 errors, {len(warnings)} warning(s))')
    else:
        print(f'RESULT: PASSED  â€” dataset looks healthy, shuffle quality is good')

    if warnings:
        for w in warnings:
            print(f'  WARN: {w}')

    print()
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
