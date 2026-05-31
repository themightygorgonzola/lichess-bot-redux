#!/usr/bin/env python3
"""
diagnose_trainer_scaling.py â€” Run trainer scaling diagnostics across sample sizes.

Purpose:
  - Reproduce full-trainer behavior on progressively larger subsets.
  - Identify where convergence degrades as sample count grows.
  - Compare loss modes and batch sizes under the real trainer, not the exact-fit harness.

Example:
  python tools/diagnose_trainer_scaling.py \
    --variant huge_relu \
    --sample-sizes 1000 2000 4000 8000 16000 32000 65536 131072 262144 \
    --batch-sizes 65536 16384 \
    --loss-modes wdl cp hybrid \
    --epochs 20 --device cuda
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA = ROOT / 'data' / 'processed' / 'mean-alltime-dedup.bin'
RESULTS_ROOT = ROOT / 'results' / 'trainer_scaling'


def _default_sample_sizes() -> list[int]:
    return [
        1_000,
        2_000,
        4_000,
        8_000,
        10_000,
        16_000,
        32_000,
        65_536,
        100_000,
        131_072,
        262_144,
        524_288,
        1_000_000,
    ]


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='Diagnose trainer convergence across sample sizes')
    ap.add_argument('--data', default=str(DEFAULT_DATA))
    ap.add_argument('--variant', default='huge_relu')
    ap.add_argument('--sample-sizes', type=int, nargs='+', default=_default_sample_sizes())
    ap.add_argument('--batch-sizes', type=int, nargs='+', default=[65536])
    ap.add_argument('--loss-modes', nargs='+', default=['wdl', 'cp', 'hybrid'], choices=['wdl', 'cp', 'hybrid'])
    ap.add_argument('--epochs', type=int, default=20)
    ap.add_argument('--target-updates', type=int, default=0,
                    help='If >0, choose epochs per run so each configuration gets at least this many optimizer updates')
    ap.add_argument('--device', default='cuda')
    ap.add_argument('--lr', type=float, default=1e-3)
    ap.add_argument('--wd', type=float, default=1e-4)
    ap.add_argument('--lambda-loss', type=float, default=0.5)
    ap.add_argument('--target-wdl-source', choices=['cp', 'stored'], default='cp')
    ap.add_argument('--cp-beta', type=float, default=100.0)
    ap.add_argument('--cp-scale', type=float, default=100.0)
    ap.add_argument('--wdl-aux-weight', type=float, default=0.25)
    ap.add_argument('--score-cap', type=int, default=10000)
    ap.add_argument('--num-workers', type=int, default=0)
    ap.add_argument('--save-every', type=int, default=0)
    ap.add_argument('--out-dir', default='')
    ap.add_argument('--continue-on-error', action='store_true')
    return ap.parse_args()


def _run(cmd: list[str]) -> int:
    print('\n$', ' '.join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(proc.returncode)


def _read_log(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open('r', newline='') as f:
        return list(csv.DictReader(f))


def _summarize_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        return {
            'epochs_completed': 0,
            'best_epoch': -1,
            'best_val_mae': None,
            'best_val_loss': None,
            'final_val_mae': None,
            'final_val_loss': None,
            'final_train_loss': None,
        }

    best_row = min(rows, key=lambda r: float(r['val_mae']))
    final_row = rows[-1]
    return {
        'epochs_completed': len(rows),
        'best_epoch': int(best_row['epoch']),
        'best_val_mae': float(best_row['val_mae']),
        'best_val_loss': float(best_row['val_loss']),
        'best_val_mse': float(best_row['val_mse']),
        'best_val_bce': float(best_row['val_bce']),
        'final_val_mae': float(final_row['val_mae']),
        'final_val_loss': float(final_row['val_loss']),
        'final_train_loss': float(final_row['train_loss']),
        'final_lr': float(final_row['lr']),
        'final_elapsed_s': float(final_row['elapsed_s']),
    }


def _epochs_for_run(sample_size: int, batch_size: int, fixed_epochs: int, target_updates: int) -> int:
    if target_updates <= 0:
        return max(1, fixed_epochs)
    n_val = max(1, sample_size // 10)
    n_train = max(1, sample_size - n_val)
    batches_per_epoch = max(1, (n_train + batch_size - 1) // batch_size)
    return max(1, (target_updates + batches_per_epoch - 1) // batches_per_epoch)


def main() -> int:
    args = _parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else (RESULTS_ROOT / f'{args.variant}_diagnose')
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []

    for batch_size in args.batch_sizes:
        for loss_mode in args.loss_modes:
            for sample_size in args.sample_sizes:
                run_name = f'{args.variant}_{loss_mode}_n{sample_size}_b{batch_size}'
                ckpt_dir = out_dir / run_name
                if ckpt_dir.exists():
                    for p in ckpt_dir.glob('*'):
                        if p.is_file():
                            p.unlink()
                ckpt_dir.mkdir(parents=True, exist_ok=True)

                epochs = _epochs_for_run(sample_size, batch_size, args.epochs, args.target_updates)
                cmd = [
                    sys.executable, '-m', 'nnue.trainer',
                    '--data', args.data,
                    '--model-variant', args.variant,
                    '--epochs', str(epochs),
                    '--batch-size', str(batch_size),
                    '--max-samples', str(sample_size),
                    '--device', args.device,
                    '--num-workers', str(args.num_workers),
                    '--checkpoint', str(ckpt_dir),
                    '--save-every', str(args.save_every),
                    '--lr', str(args.lr),
                    '--wd', str(args.wd),
                    '--lambda-loss', str(args.lambda_loss),
                    '--target-wdl-source', str(args.target_wdl_source),
                    '--loss-mode', str(loss_mode),
                    '--cp-beta', str(args.cp_beta),
                    '--cp-scale', str(args.cp_scale),
                    '--wdl-aux-weight', str(args.wdl_aux_weight),
                    '--score-cap', str(args.score_cap),
                ]

                t0 = time.time()
                rc = _run(cmd)
                elapsed = time.time() - t0
                log_rows = _read_log(ckpt_dir / 'training_log.csv')
                summary = _summarize_rows(log_rows)
                row = {
                    'variant': args.variant,
                    'loss_mode': loss_mode,
                    'sample_size': int(sample_size),
                    'batch_size': int(batch_size),
                    'epochs_requested': int(epochs),
                    'exit_code': int(rc),
                    'wall_s': float(elapsed),
                    'checkpoint': str(ckpt_dir.relative_to(ROOT)),
                }
                row.update(summary)
                rows.append(row)

                if rc != 0 and not args.continue_on_error:
                    summary_path = out_dir / 'summary_partial.json'
                    summary_path.write_text(json.dumps(rows, indent=2), encoding='utf-8')
                    raise SystemExit(rc)

    json_path = out_dir / 'summary.json'
    csv_path = out_dir / 'summary.csv'
    json_path.write_text(json.dumps(rows, indent=2), encoding='utf-8')

    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with csv_path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f'\nWrote {json_path}')
    print(f'Wrote {csv_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
