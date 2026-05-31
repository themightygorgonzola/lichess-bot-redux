"""
fit_binary_subset.py â€” Memorization / fit-capacity test on a sampled NNUE binary subset.

Purpose:
  - Sample N records from a .bin dataset deterministically.
  - Train the current NNUE model only on that subset.
  - Measure whether the model can drive train error down to a requested threshold.

This is the right tool for questions like:
  - Can the current architecture fit 10,000 positions to 0.1 cp?
  - Can it fit 1,000,000 positions to 1 cp?

Usage examples:
  python tools/fit_binary_subset.py --sample-size 10000 --target-mae 0.1 --target-maxerr 0.1
  python tools/fit_binary_subset.py --sample-size 1000000 --batch-size 65536 --target-mae 1 --target-maxerr 1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.data import RECORD_DTYPE, HEADER_SIZE, _read_header
from ml.experimental_model import available_variants, build_model, model_param_count

DEFAULT_DATA = ROOT / 'data' / 'processed' / 'mean-alltime-dedup.bin'


def _sample_records(path: Path, sample_size: int, seed: int) -> np.ndarray:
    meta = _read_header(str(path))
    n = meta['n_records']
    if sample_size > n:
        raise ValueError(f'sample_size={sample_size:,} exceeds dataset size {n:,}')
    arr = np.memmap(str(path), dtype=RECORD_DTYPE, mode='r', offset=HEADER_SIZE, shape=(n,))
    rng = random.Random(seed)
    idx = np.fromiter(sorted(rng.sample(range(n), sample_size)), dtype=np.int64, count=sample_size)
    idx.sort()
    return np.array(arr[idx], copy=True)


def _load_or_create_sample(path: Path, sample_size: int, seed: int,
                           cache_path: Path | None) -> np.ndarray:
    if cache_path is not None and cache_path.is_file():
        arr = np.load(cache_path, allow_pickle=False)
        if len(arr) != sample_size:
            raise ValueError(f'Cached sample size mismatch: expected {sample_size}, got {len(arr)}')
        return arr

    arr = _sample_records(path, sample_size, seed)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache_path, arr, allow_pickle=False)
    return arr


def _to_tensors(arr: np.ndarray, device: torch.device) -> dict[str, torch.Tensor]:
    stm_arr = arr['stm'].astype(np.float32)
    score_arr = arr['score'].astype(np.float32)
    score_stm = np.where(stm_arr == 1.0, -score_arr, score_arr)
    return {
        'white_idx': torch.from_numpy(arr['white_feats'].astype(np.int32)).to(device=device),
        'white_cnt': torch.from_numpy(arr['n_white'].astype(np.int32)).to(device=device),
        'black_idx': torch.from_numpy(arr['black_feats'].astype(np.int32)).to(device=device),
        'black_cnt': torch.from_numpy(arr['n_black'].astype(np.int32)).to(device=device),
        'stm': torch.from_numpy(arr['stm'].astype(np.int64)).to(device=device),
        'bucket': torch.from_numpy(arr['bucket'].astype(np.int64)).to(device=device),
        'target': torch.from_numpy(score_stm.astype(np.float32)).to(device=device),
    }


def _forward_model(model: torch.nn.Module, batch: dict[str, torch.Tensor], idx: torch.Tensor | None = None) -> torch.Tensor:
    if idx is None:
        return model(
            batch['white_idx'], batch['white_cnt'],
            batch['black_idx'], batch['black_cnt'],
            batch['stm'], batch['bucket'],
        ).squeeze(1)
    return model(
        batch['white_idx'][idx], batch['white_cnt'][idx],
        batch['black_idx'][idx], batch['black_cnt'][idx],
        batch['stm'][idx], batch['bucket'][idx],
    ).squeeze(1)


def _evaluate(model: torch.nn.Module, batch: dict[str, torch.Tensor]) -> tuple[float, float, float, np.ndarray]:
    model.eval()
    with torch.no_grad():
        pred = _forward_model(model, batch)
        diff = pred - batch['target']
        mae = float(torch.mean(torch.abs(diff)).item())
        max_err = float(torch.max(torch.abs(diff)).item())
        rmse = float(torch.sqrt(torch.mean(diff * diff)).item())
        pred_cpu = pred.detach().cpu().numpy()
    model.train()
    return mae, max_err, rmse, pred_cpu


def main() -> int:
    ap = argparse.ArgumentParser(description='Fit the current NNUE model to a sampled binary subset')
    ap.add_argument('--data', default=str(DEFAULT_DATA), help='Path to input .bin file')
    ap.add_argument('--sample-size', type=int, required=True, help='Number of records to sample')
    ap.add_argument('--sample-seed', type=int, default=0, help='Deterministic subset seed')
    ap.add_argument('--sample-cache', default='', help='Optional .npy cache path for the sampled subset')
    ap.add_argument('--device', default='auto', help='cuda/cpu/auto')
    ap.add_argument('--model-variant', choices=available_variants(), default='baseline', help='Architecture variant to test')
    ap.add_argument('--lr', type=float, default=0.01, help='Learning rate')
    ap.add_argument('--weight-decay', type=float, default=0.0, help='Weight decay')
    ap.add_argument('--batch-size', type=int, default=0, help='Mini-batch size (0 = full batch)')
    ap.add_argument('--max-epochs', type=int, default=20000, help='Maximum training epochs')
    ap.add_argument('--target-mae', type=float, default=0.1, help='Pass threshold for mean abs error in cp')
    ap.add_argument('--target-maxerr', type=float, default=0.1, help='Pass threshold for worst abs error in cp')
    ap.add_argument('--print-every', type=int, default=25, help='Progress print interval')
    ap.add_argument('--history-mode', choices=['sparse', 'full'], default='sparse', help='Whether to store sparse checkpoints or every epoch in result history')
    ap.add_argument('--output-json', default='', help='Optional JSON path for results')
    args = ap.parse_args()

    data_path = Path(args.data)
    if not data_path.is_file():
        raise SystemExit(f'Dataset not found: {data_path}')

    if args.device == 'auto':
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device(args.device)

    print(f'Device: {device}')
    print(f'Dataset: {data_path}')
    print(f'Sample size: {args.sample_size:,}')
    print(f'Sample seed: {args.sample_seed}')
    print(f'Model variant: {args.model_variant}')
    print(f'LR: {args.lr}')
    print(f'Weight decay: {args.weight_decay}')
    print(f'Batch size: {args.batch_size or args.sample_size:,}')

    t_load = time.time()
    cache_path = Path(args.sample_cache) if args.sample_cache.strip() else None
    arr = _load_or_create_sample(data_path, args.sample_size, args.sample_seed, cache_path)
    batch = _to_tensors(arr, device)
    print(f'Loaded sample in {time.time() - t_load:.2f}s')
    if cache_path is not None:
        print(f'Sample cache: {cache_path}')

    model = build_model(args.model_variant).to(device)
    model.train()
    print(f'Parameters: {model_param_count(model):,}')
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    n = args.sample_size
    bs = args.batch_size if args.batch_size and args.batch_size > 0 else n
    success = False
    final_pred = None
    t0 = time.time()
    best_mae = float('inf')
    best_max_err = float('inf')
    best_rmse = float('inf')
    best_epoch = -1
    pass_epoch = -1
    mae_lt_10 = -1
    mae_lt_1 = -1
    mae_lt_01 = -1
    maxerr_lt_10 = -1
    maxerr_lt_1 = -1
    maxerr_lt_01 = -1
    regression_count = 0
    large_spike_count = 0
    prev_mae = None
    history = []

    for epoch in range(1, args.max_epochs + 1):
        perm = torch.randperm(n, device=device) if bs < n else None
        total_loss = 0.0
        n_steps = 0

        if bs >= n:
            optimizer.zero_grad(set_to_none=True)
            pred = _forward_model(model, batch)
            diff = pred - batch['target']
            loss = torch.mean(diff * diff)
            loss.backward()
            optimizer.step()
            total_loss = float(loss.item())
            n_steps = 1
        else:
            for start in range(0, n, bs):
                idx = perm[start:start + bs]
                optimizer.zero_grad(set_to_none=True)
                pred = _forward_model(model, batch, idx)
                diff = pred - batch['target'][idx]
                loss = torch.mean(diff * diff)
                loss.backward()
                optimizer.step()
                total_loss += float(loss.item())
                n_steps += 1

        mae, max_err, rmse, final_pred = _evaluate(model, batch)
        avg_loss = total_loss / max(n_steps, 1)

        if mae < best_mae:
            best_mae = mae
            best_max_err = max_err
            best_rmse = rmse
            best_epoch = epoch
        if mae_lt_10 < 0 and mae <= 10.0:
            mae_lt_10 = epoch
        if mae_lt_1 < 0 and mae <= 1.0:
            mae_lt_1 = epoch
        if mae_lt_01 < 0 and mae <= 0.1:
            mae_lt_01 = epoch
        if maxerr_lt_10 < 0 and max_err <= 10.0:
            maxerr_lt_10 = epoch
        if maxerr_lt_1 < 0 and max_err <= 1.0:
            maxerr_lt_1 = epoch
        if maxerr_lt_01 < 0 and max_err <= 0.1:
            maxerr_lt_01 = epoch
        if prev_mae is not None:
            if mae > prev_mae:
                regression_count += 1
            if prev_mae <= 1.0 and mae > prev_mae * 1.5:
                large_spike_count += 1
        prev_mae = mae

        should_record = args.history_mode == 'full' or epoch % args.print_every == 0 or epoch == 1
        if should_record:
            history.append({
                'epoch': int(epoch),
                'loss': float(avg_loss),
                'mae': float(mae),
                'max_err': float(max_err),
                'rmse': float(rmse),
            })

        if epoch % args.print_every == 0 or epoch == 1:
            print(f'epoch={epoch:>6d}  loss={avg_loss:>12.6f}  mae={mae:>10.4f}  max_err={max_err:>10.4f}  rmse={rmse:>10.4f}')

        if mae <= args.target_mae and max_err <= args.target_maxerr:
            success = True
            pass_epoch = epoch
            print(f'\nPASS at epoch {epoch}: mae={mae:.4f}cp  max_err={max_err:.4f}cp  rmse={rmse:.4f}cp')
            break

    elapsed = time.time() - t0
    print(f'\nElapsed: {elapsed:.2f}s')

    with torch.no_grad():
        target_cpu = batch['target'].detach().cpu().numpy()
    top_idx = np.argsort(np.abs(final_pred - target_cpu))[::-1][:10]
    print('Worst 10 errors')
    for rank, i in enumerate(top_idx, 1):
        err = float(final_pred[i] - target_cpu[i])
        print(f'  #{rank:>2d}  idx={i:>7d}  target={target_cpu[i]:>9.2f}  pred={final_pred[i]:>9.2f}  err={err:>9.3f}')

    result = {
        'data': str(data_path),
        'sample_size': int(args.sample_size),
        'sample_seed': int(args.sample_seed),
        'sample_cache': str(cache_path) if cache_path is not None else '',
        'device': str(device),
        'model_variant': args.model_variant,
        'lr': float(args.lr),
        'weight_decay': float(args.weight_decay),
        'batch_size': int(bs),
        'max_epochs': int(args.max_epochs),
        'target_mae': float(args.target_mae),
        'target_maxerr': float(args.target_maxerr),
        'success': bool(success),
        'pass_epoch': int(pass_epoch),
        'elapsed_s': float(elapsed),
        'best_mae': float(best_mae),
        'best_max_err': float(best_max_err),
        'best_rmse': float(best_rmse),
        'best_epoch': int(best_epoch),
        'final_mae': float(np.mean(np.abs(final_pred - target_cpu))),
        'final_max_err': float(np.max(np.abs(final_pred - target_cpu))),
        'final_rmse': float(np.sqrt(np.mean((final_pred - target_cpu) ** 2))),
        'epoch_mae_le_10': int(mae_lt_10),
        'epoch_mae_le_1': int(mae_lt_1),
        'epoch_mae_le_0_1': int(mae_lt_01),
        'epoch_maxerr_le_10': int(maxerr_lt_10),
        'epoch_maxerr_le_1': int(maxerr_lt_1),
        'epoch_maxerr_le_0_1': int(maxerr_lt_01),
        'mae_regression_count': int(regression_count),
        'large_spike_count': int(large_spike_count),
        'history_mode': args.history_mode,
        'history': history,
    }

    output_json = args.output_json.strip()
    if output_json:
        out_path = Path(output_json)
    else:
        out_path = ROOT / 'results' / f'fit_subset_{args.sample_size}_{args.sample_seed}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f'Wrote {out_path}')

    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
