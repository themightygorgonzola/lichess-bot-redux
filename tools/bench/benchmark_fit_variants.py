"""
benchmark_fit_variants.py â€” Architecture benchmark suite for NNUE experiments.

This script compares many model variants on the same cached subset and reports:
  - fit viability on the memorization task
  - convergence speed (epochs / seconds to threshold)
  - stability signals (regression counts, large spikes)
  - forward eval throughput (positions/s, NPS proxy)
  - train-step throughput (samples/s)

The fit task uses tools/fit_binary_subset.py so every architecture is tested on the
exact same sampled positions. Speed tests reuse the same cached sample directly.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.experimental_model import available_variants, build_model, model_param_count
from tools.fit_binary_subset import _forward_model, _load_or_create_sample, _to_tensors

FIT_SCRIPT = ROOT / 'tools' / 'fit_binary_subset.py'
RESULTS_DIR = ROOT / 'results' / 'arch_bench'


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='Benchmark multiple NNUE variants on the same fit and speed tasks')
    ap.add_argument('--data', default=str(ROOT / 'data' / 'processed' / 'mean-alltime-dedup.bin'))
    ap.add_argument('--sample-size', type=int, default=10000)
    ap.add_argument('--sample-seed', type=int, default=0)
    ap.add_argument('--sample-cache', default='', help='Optional .npy cache path; default is results/arch_bench/sample_<n>_<seed>.npy')
    ap.add_argument('--device', default='cuda')
    ap.add_argument('--variants', nargs='+', required=True, choices=available_variants())
    ap.add_argument('--lr', type=float, default=0.01)
    ap.add_argument('--weight-decay', type=float, default=0.0)
    ap.add_argument('--batch-size', type=int, default=0)
    ap.add_argument('--max-epochs', type=int, default=5000)
    ap.add_argument('--target-mae', type=float, default=0.1)
    ap.add_argument('--target-maxerr', type=float, default=0.1)
    ap.add_argument('--print-every', type=int, default=25)
    ap.add_argument('--fit-timeout', type=int, default=0, help='Per-variant fit timeout in seconds (0 = no timeout)')
    ap.add_argument('--reuse-fit-results', action='store_true', help='Reuse existing per-variant fit JSON if present')
    ap.add_argument('--skip-fit', action='store_true')
    ap.add_argument('--skip-speed', action='store_true')
    ap.add_argument('--speed-batch-sizes', type=int, nargs='+', default=[1024, 4096, 16384, 65536])
    ap.add_argument('--speed-warmup', type=int, default=20)
    ap.add_argument('--speed-iters', type=int, default=100)
    ap.add_argument('--train-speed-iters', type=int, default=25)
    ap.add_argument('--summary-prefix', default='', help='Optional prefix for summary filenames')
    return ap.parse_args()


def _sync(device: torch.device) -> None:
    if device.type == 'cuda':
        torch.cuda.synchronize(device)


def _device_name(device: torch.device) -> str:
    if device.type == 'cuda':
        return torch.cuda.get_device_name(device)
    return str(device)


def _ensure_cache(args: argparse.Namespace) -> Path:
    if args.sample_cache.strip():
        cache_path = Path(args.sample_cache)
    else:
        cache_path = RESULTS_DIR / f'sample_{args.sample_size}_{args.sample_seed}.npy'
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _load_or_create_sample(Path(args.data), args.sample_size, args.sample_seed, cache_path)
    return cache_path


def _run_fit_benchmark(args: argparse.Namespace, variant: str, sample_cache: Path) -> dict[str, Any]:
    out_json = RESULTS_DIR / f'{variant}_{args.sample_size}_{args.sample_seed}.json'
    if args.reuse_fit_results and out_json.is_file():
        row: dict[str, Any] = {
            'variant': variant,
            'fit_exit_code': 0,
            'fit_timed_out': False,
            'fit_result_json': str(out_json),
            'fit_wall_s': 0.0,
            'fit_reused': True,
        }
        row.update(json.loads(out_json.read_text(encoding='utf-8')))
        return row

    cmd = [
        sys.executable, str(FIT_SCRIPT),
        '--data', args.data,
        '--sample-size', str(args.sample_size),
        '--sample-seed', str(args.sample_seed),
        '--sample-cache', str(sample_cache),
        '--device', args.device,
        '--model-variant', variant,
        '--lr', str(args.lr),
        '--weight-decay', str(args.weight_decay),
        '--batch-size', str(args.batch_size),
        '--max-epochs', str(args.max_epochs),
        '--target-mae', str(args.target_mae),
        '--target-maxerr', str(args.target_maxerr),
        '--print-every', str(args.print_every),
        '--history-mode', 'full',
        '--output-json', str(out_json),
    ]

    t0 = time.time()
    try:
        ret = subprocess.run(
            cmd,
            cwd=str(ROOT),
            check=False,
            timeout=(None if args.fit_timeout <= 0 else args.fit_timeout),
        )
        exit_code = int(ret.returncode)
        timed_out = False
    except subprocess.TimeoutExpired:
        exit_code = 124
        timed_out = True

    row: dict[str, Any] = {
        'variant': variant,
        'fit_exit_code': exit_code,
        'fit_timed_out': timed_out,
        'fit_result_json': str(out_json),
        'fit_wall_s': float(time.time() - t0),
        'fit_reused': False,
    }
    if out_json.is_file():
        row.update(json.loads(out_json.read_text(encoding='utf-8')))
    return row


def _tensor_slice(batch: dict[str, torch.Tensor], batch_size: int) -> dict[str, torch.Tensor]:
    return {k: v[:batch_size] for k, v in batch.items()}


def _benchmark_forward_once(model: torch.nn.Module, batch: dict[str, torch.Tensor], batch_size: int,
                            warmup: int, iters: int, device: torch.device) -> dict[str, Any]:
    sb = _tensor_slice(batch, batch_size)
    model.eval()
    with torch.no_grad():
        for _ in range(warmup):
            _forward_model(model, sb)
        _sync(device)
        t0 = time.perf_counter()
        for _ in range(iters):
            _forward_model(model, sb)
        _sync(device)
    elapsed = time.perf_counter() - t0
    pos_per_sec = (batch_size * iters) / max(elapsed, 1e-12)
    return {
        'batch_size': int(batch_size),
        'iters': int(iters),
        'elapsed_s': float(elapsed),
        'latency_ms': float((elapsed / max(iters, 1)) * 1000.0),
        'positions_per_sec': float(pos_per_sec),
        'nps_proxy': float(pos_per_sec),
    }


def _benchmark_train_once(model_variant: str, batch: dict[str, torch.Tensor], batch_size: int,
                          warmup: int, iters: int, device: torch.device, lr: float,
                          weight_decay: float) -> dict[str, Any]:
    sb = _tensor_slice(batch, batch_size)
    model = build_model(model_variant).to(device)
    model.train()
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)

    def _step() -> None:
        optimizer.zero_grad(set_to_none=True)
        pred = _forward_model(model, sb)
        diff = pred - sb['target']
        loss = torch.mean(diff * diff)
        loss.backward()
        optimizer.step()

    for _ in range(warmup):
        _step()
    _sync(device)
    t0 = time.perf_counter()
    for _ in range(iters):
        _step()
    _sync(device)
    elapsed = time.perf_counter() - t0
    samples_per_sec = (batch_size * iters) / max(elapsed, 1e-12)
    return {
        'batch_size': int(batch_size),
        'iters': int(iters),
        'elapsed_s': float(elapsed),
        'step_ms': float((elapsed / max(iters, 1)) * 1000.0),
        'train_samples_per_sec': float(samples_per_sec),
    }


def _benchmark_speed(args: argparse.Namespace, variant: str, batch: dict[str, torch.Tensor],
                     device: torch.device) -> dict[str, Any]:
    model = build_model(variant).to(device)
    param_count = model_param_count(model)
    n = int(batch['target'].shape[0])
    batch_sizes = sorted({min(max(1, bs), n) for bs in args.speed_batch_sizes})

    forward = [
        _benchmark_forward_once(model, batch, bs, args.speed_warmup, args.speed_iters, device)
        for bs in batch_sizes
    ]
    train = [
        _benchmark_train_once(variant, batch, bs, max(1, args.speed_warmup // 2), args.train_speed_iters,
                              device, args.lr, args.weight_decay)
        for bs in batch_sizes
    ]

    best_forward = max(forward, key=lambda x: x['positions_per_sec']) if forward else None
    best_train = max(train, key=lambda x: x['train_samples_per_sec']) if train else None
    return {
        'variant': variant,
        'param_count': int(param_count),
        'forward_benchmarks': forward,
        'train_benchmarks': train,
        'best_forward_positions_per_sec': float(best_forward['positions_per_sec']) if best_forward else 0.0,
        'best_forward_batch_size': int(best_forward['batch_size']) if best_forward else 0,
        'best_nps_proxy': float(best_forward['nps_proxy']) if best_forward else 0.0,
        'best_train_samples_per_sec': float(best_train['train_samples_per_sec']) if best_train else 0.0,
        'best_train_batch_size': int(best_train['batch_size']) if best_train else 0,
    }


def _merge_rows(fit_row: dict[str, Any] | None, speed_row: dict[str, Any] | None,
                variant: str, device: torch.device, sample_cache: Path) -> dict[str, Any]:
    row: dict[str, Any] = {
        'variant': variant,
        'device': str(device),
        'device_name': _device_name(device),
        'sample_cache': str(sample_cache),
    }
    if fit_row:
        row.update(fit_row)
    if speed_row:
        row.update(speed_row)

    history = row.get('history') if isinstance(row.get('history'), list) else []
    pass_epoch = int(row.get('pass_epoch', -1) or -1)
    elapsed_s = float(row.get('elapsed_s', 0.0) or 0.0)
    row['time_to_target_s'] = float(elapsed_s if pass_epoch > 0 else 0.0)

    regressions = int(row.get('mae_regression_count', 0) or 0)
    spikes = int(row.get('large_spike_count', 0) or 0)
    denom = max(pass_epoch if pass_epoch > 0 else int(row.get('max_epochs', 1) or 1), 1)
    row['mae_regression_rate'] = float(regressions / denom)

    if history:
        maes = [float(h['mae']) for h in history]
        max_errs = [float(h['max_err']) for h in history]
        best_idx = min(range(len(maes)), key=lambda i: maes[i])
        best_mae_hist = maes[best_idx]
        final_mae = maes[-1]
        post_best_maes = maes[best_idx:]
        post_best_max_errs = max_errs[best_idx:]
        rebound_ratio = max(post_best_maes) / max(best_mae_hist, 1e-9)
        final_to_best_mae_ratio = final_mae / max(best_mae_hist, 1e-9)
        instability_events = 0
        settled_epoch = int(row.get('epoch_mae_le_1', -1) or -1)
        if settled_epoch > 0:
            start = max(settled_epoch - 1, 1)
            for i in range(start, len(maes)):
                if maes[i] > maes[i - 1] * 1.25:
                    instability_events += 1
        auc_log_mae = float(sum(np.log10(max(v, 1e-6)) for v in maes) / len(maes))
        row['history_epochs'] = int(len(history))
        row['history_best_epoch'] = int(history[best_idx]['epoch'])
        row['history_best_mae'] = float(best_mae_hist)
        row['post_best_mae_rebound_ratio'] = float(rebound_ratio)
        row['final_to_best_mae_ratio'] = float(final_to_best_mae_ratio)
        row['post_best_max_err'] = float(max(post_best_max_errs))
        row['instability_events'] = int(instability_events)
        row['auc_log10_mae'] = auc_log_mae
    else:
        row['history_epochs'] = 0
        row['history_best_epoch'] = -1
        row['history_best_mae'] = float(row.get('best_mae', 0.0) or 0.0)
        row['post_best_mae_rebound_ratio'] = 1.0
        row['final_to_best_mae_ratio'] = 1.0
        row['post_best_max_err'] = float(row.get('final_max_err', 0.0) or 0.0)
        row['instability_events'] = 0
        row['auc_log10_mae'] = 0.0

    row['stability_penalty'] = float(
        float(row.get('instability_events', 0) or 0) * 50
        + float(row.get('large_spike_count', 0) or 0) * 10
        + max(0.0, float(row.get('post_best_mae_rebound_ratio', 1.0) or 1.0) - 1.0) * 25
        + max(0.0, float(row.get('final_to_best_mae_ratio', 1.0) or 1.0) - 1.0) * 10
    )
    return row


def _write_summaries(rows: list[dict[str, Any]], prefix: str) -> tuple[Path, Path]:
    summary_csv = RESULTS_DIR / f'{prefix}.csv'
    summary_json = RESULTS_DIR / f'{prefix}.json'

    csv_rows = []
    for row in rows:
        flat = {}
        for key, value in row.items():
            if isinstance(value, (list, dict)):
                continue
            flat[key] = value
        csv_rows.append(flat)

    with open(summary_csv, 'w', newline='', encoding='utf-8') as f:
        fieldnames = sorted({k for row in csv_rows for k in row.keys()})
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    summary_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')
    return summary_csv, summary_json


def main() -> int:
    args = _parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    device = torch.device('cuda' if args.device == 'auto' and torch.cuda.is_available() else args.device)
    sample_cache = _ensure_cache(args)

    speed_batch = None
    if not args.skip_speed:
        arr = np.load(sample_cache, allow_pickle=False)
        speed_batch = _to_tensors(arr, device)

    rows = []
    print(f'Device: {device} ({_device_name(device)})')
    print(f'Sample cache: {sample_cache}')
    print(f'Variants: {", ".join(args.variants)}')

    for variant in args.variants:
        print(f'\n===== {variant} =====')
        fit_row = None
        speed_row = None
        if not args.skip_fit:
            print('Running fit benchmark...')
            fit_row = _run_fit_benchmark(args, variant, sample_cache)
        if not args.skip_speed:
            print('Running speed benchmark...')
            speed_row = _benchmark_speed(args, variant, speed_batch, device)
        row = _merge_rows(fit_row, speed_row, variant, device, sample_cache)
        rows.append(row)
        print(
            f"Summary {variant}: "
            f"best_mae={row.get('best_mae', 0.0):.4f}, "
            f"pass_epoch={row.get('pass_epoch', -1)}, "
            f"best_nps_proxy={row.get('best_nps_proxy', 0.0):,.0f}, "
            f"stability_penalty={row.get('stability_penalty', 0.0):.1f}"
        )

    prefix = args.summary_prefix.strip() or f'bench_{args.sample_size}_{args.sample_seed}'
    summary_csv, summary_json = _write_summaries(rows, prefix)
    print(f'\nWrote {summary_csv}')
    print(f'Wrote {summary_json}')
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONUNBUFFERED', '1')
    raise SystemExit(main())
