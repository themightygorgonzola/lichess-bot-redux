"""
trainer.py — AMP-enabled training loop for NNUE v2.

Features:
  - Mixed-precision training (float16 on GPU, float32 master weights)
  - Cosine annealing with warm restarts
  - Gradient clipping
  - Periodic checkpointing + best-model tracking
  - CSV training log
  - Auto-export nn.bin on best improvement
"""

import argparse
import csv as csv_mod
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, random_split
from torch.amp import autocast, GradScaler
try:
    from torch.utils.tensorboard import SummaryWriter as _SummaryWriter
    _TB_AVAILABLE = True
except ImportError:
    _TB_AVAILABLE = False

from .arch import INPUT_SIZE, FT_SIZE, L1_SIZE, L2_SIZE, OUTPUT_BUCKETS
from .model import NNUE, count_parameters, model_summary
from .experimental_model import available_variants, build_model, model_param_count
from .loss import training_loss, wdl_eval_metrics, target_win_probability, WDL_SCALE, BUCKET_WDL_SCALES


# ── Train / Eval loops ────────────────────────────────────────────────────

def train_epoch(model, loader, optimizer, scaler, device, lambda_loss, max_grad_norm,
               n_total_batches: int = 0, print_every: int = 10,
               target_wdl_source: str = 'cp', loss_mode: str = 'wdl',
               cp_beta: float = 100.0, cp_scale: float = 100.0,
               wdl_aux_weight: float = 0.25,
               wdl_scale=None,
               color_flip_aug: bool = False,
               color_flip_rng=None):
    model.train()
    total_loss = 0.0
    n_batches = 0
    t0 = time.time()

    for white_idx, white_cnt, black_idx, black_cnt, stm, scores, wdl, buckets in loader:
        white_idx = white_idx.to(device, non_blocking=True)
        white_cnt = white_cnt.to(device, non_blocking=True)
        black_idx = black_idx.to(device, non_blocking=True)
        black_cnt = black_cnt.to(device, non_blocking=True)
        stm     = stm.to(device, non_blocking=True)
        scores  = scores.to(device, non_blocking=True)
        wdl     = wdl.to(device, non_blocking=True)
        buckets = buckets.to(device, non_blocking=True)

        # Color-flip augmentation: with 50% probability, swap sides.
        # Negates score, inverts WDL, swaps white/black feature tensors.
        # Piece counts (and thus buckets) are color-symmetric — no change needed.
        if color_flip_aug and color_flip_rng is not None:
            flip_mask = torch.from_numpy(
                color_flip_rng.integers(0, 2, size=scores.shape[0], dtype=np.uint8).astype(bool)
            ).to(device)
            if flip_mask.any():
                white_idx_f  = torch.where(flip_mask.unsqueeze(1), black_idx,  white_idx)
                black_idx_f  = torch.where(flip_mask.unsqueeze(1), white_idx,  black_idx)
                white_cnt_f  = torch.where(flip_mask, black_cnt,  white_cnt)
                black_cnt_f  = torch.where(flip_mask, white_cnt,  black_cnt)
                white_idx, black_idx = white_idx_f, black_idx_f
                white_cnt, black_cnt = white_cnt_f, black_cnt_f
                stm    = torch.where(flip_mask, 1 - stm,  stm)
                scores = torch.where(flip_mask, -scores, scores)
                wdl    = torch.where(flip_mask, 1.0 - wdl, wdl)

        optimizer.zero_grad(set_to_none=True)

        with autocast(device_type=device.type, dtype=torch.float16, enabled=(device.type == 'cuda')):
            pred = model(white_idx, white_cnt, black_idx, black_cnt, stm, buckets).squeeze(1)
            loss = training_loss(
                pred, scores, wdl,
                mode=loss_mode,
                lambda_=lambda_loss,
                target_wdl_source=target_wdl_source,
                cp_beta=cp_beta,
                cp_scale=cp_scale,
                wdl_aux_weight=wdl_aux_weight,
                scale=wdl_scale if wdl_scale is not None else WDL_SCALE,
                buckets=buckets if wdl_scale is not None else None,
            )

        if scaler is not None:
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
            optimizer.step()

        total_loss += loss.item()
        n_batches += 1

        if print_every > 0 and n_batches % print_every == 0:
            elapsed = time.time() - t0
            bps = n_batches / max(elapsed, 1e-6)
            eta = (n_total_batches - n_batches) / max(bps, 1e-6) if n_total_batches > 0 else 0
            avg = total_loss / n_batches
            # Sub-cell progress bar: ░▒▓ encode the fractional position of the leading edge
            BAR = 35
            cells = (n_batches / n_total_batches * BAR) if n_total_batches else 0
            n_full = int(cells)
            partial = ' ░▒▓'[min(3, int((cells - n_full) * 4))]
            bar = '█' * n_full + partial + ' ' * (BAR - n_full - 1)
            print(f"\r        \u2514\u2500 [{bar}] {n_batches:>5}/{n_total_batches}"
                  f"  loss {avg:>12,.5f}  {bps:4.1f} b/s  ETA {int(eta):>4d}s  ",
                  end='', flush=True)

    # Clear the progress line before returning
    if print_every > 0 and n_total_batches > 0:
        print('\r' + ' ' * 90 + '\r', end='', flush=True)

    return total_loss / max(n_batches, 1)


@torch.no_grad()
def validate(model, loader, device, lambda_loss, target_wdl_source: str = 'cp',
             loss_mode: str = 'wdl', cp_beta: float = 100.0,
             cp_scale: float = 100.0, wdl_aux_weight: float = 0.25,
             wdl_scale=None):
    model.eval()
    total_loss = 0.0
    total_mse  = 0.0
    total_bce  = 0.0
    total_mae  = 0.0
    n_batches  = 0
    n_samples  = 0

    for white_idx, white_cnt, black_idx, black_cnt, stm, scores, wdl, buckets in loader:
        white_idx = white_idx.to(device, non_blocking=True)
        white_cnt = white_cnt.to(device, non_blocking=True)
        black_idx = black_idx.to(device, non_blocking=True)
        black_cnt = black_cnt.to(device, non_blocking=True)
        stm     = stm.to(device, non_blocking=True)
        scores  = scores.to(device, non_blocking=True)
        wdl     = wdl.to(device, non_blocking=True)
        buckets = buckets.to(device, non_blocking=True)

        with autocast(device_type=device.type, dtype=torch.float16, enabled=(device.type == 'cuda')):
            pred = model(white_idx, white_cnt, black_idx, black_cnt, stm, buckets).squeeze(1)
            loss = training_loss(
                pred, scores, wdl,
                mode=loss_mode,
                lambda_=lambda_loss,
                target_wdl_source=target_wdl_source,
                cp_beta=cp_beta,
                cp_scale=cp_scale,
                wdl_aux_weight=wdl_aux_weight,
                scale=wdl_scale if wdl_scale is not None else WDL_SCALE,
                buckets=buckets if wdl_scale is not None else None,
            )

        # Compute MSE and BCE separately for diagnostics (no grad, fp32 safe)
        with torch.no_grad():
            p32 = pred.float()
            s32 = scores.float()
            mse_val = torch.mean((p32 - s32) ** 2).item()
            pred_logits  = (p32 / WDL_SCALE)
            blended = target_win_probability(
                s32,
                target_wdl=wdl,
                source=target_wdl_source,
                scale=WDL_SCALE,
            )
            bce_val = F.binary_cross_entropy_with_logits(
                pred_logits, blended.detach(), reduction='mean').item()

        total_loss += loss.item()
        total_mse  += mse_val
        total_bce  += bce_val
        total_mae  += torch.abs(p32 - s32).sum().item()
        n_batches  += 1
        n_samples  += scores.size(0)

    return (total_loss / max(n_batches, 1),
            total_mae  / max(n_samples, 1),
            total_mse  / max(n_batches, 1),
            total_bce  / max(n_batches, 1))


# ── Checkpoint helpers ────────────────────────────────────────────────────

def _save_checkpoint(path, model, optimizer, scheduler, scaler, epoch, val_loss, val_mae):
    arch = {
        'variant': getattr(model, '_variant_name', 'baseline'),
        'export_compatible': isinstance(model, NNUE),
    }
    if isinstance(model, NNUE):
        arch.update({
            'input_size': INPUT_SIZE,
            'ft_size': FT_SIZE,
            'l1_size': L1_SIZE,
            'l2_size': L2_SIZE,
            'output_buckets': OUTPUT_BUCKETS,
        })
    elif hasattr(model, 'spec'):
        arch.update({
            'input_size': INPUT_SIZE,
            'ft_size': int(model.spec.ft_size),
            'hidden_sizes': [int(x) for x in model.spec.hidden_sizes],
            'activation': str(model.spec.activation),
            'ft_activation': str(model.spec.ft_activation),
            'output_buckets': OUTPUT_BUCKETS,
        })
    state = {
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict': scheduler.state_dict() if scheduler else None,
        'scaler_state_dict': scaler.state_dict() if scaler else None,
        'val_loss': val_loss,
        'val_mae': val_mae,
        'arch': arch,
    }
    torch.save(state, path)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Train NNUE v3 (HalfKAv2 + SCReLU + L1=128 + L3 + buckets)")
    parser.add_argument("--data",         required=True, nargs='+', help="Training .bin path(s). Multiple files are interleaved each epoch.")
    parser.add_argument("--model-variant", choices=available_variants(), default="baseline",
                        help="Model variant to train. 'baseline' is the current engine-compatible v5 model.")
    parser.add_argument("--val-data",     default=None,  help="Validation CSV (auto 10% split if omitted)")
    parser.add_argument("--epochs",       type=int,   default=300)
    parser.add_argument("--batch-size",   type=int,   default=4096)
    parser.add_argument("--lr",           type=float, default=1e-3)
    parser.add_argument("--wd",           type=float, default=1e-4)
    parser.add_argument("--lambda-loss",  type=float, default=0.5,
                        help="MSE vs WDL blend (0.5 recommended)")
    parser.add_argument("--score-cap",    type=int,   default=10000)
    parser.add_argument("--max-grad-norm", type=float, default=1.0,
                        help="Maximum gradient norm for clipping")
    parser.add_argument("--checkpoint",   default="ml/checkpoints",
                        help="Checkpoint directory")
    parser.add_argument("--save-every",   type=int,   default=10)
    parser.add_argument("--export-best",  action="store_true",
                        help="Auto-export nn.bin on best improvement")
    parser.add_argument("--export-path",  default="nn_v2.bin")
    parser.add_argument("--resume",       default=None)
    parser.add_argument("--patience",     type=int,   default=0,
                        help="Early stop patience (0 = disabled; recommended for cosine-restart schedules)")
    parser.add_argument("--max-samples",  type=int,   default=0)
    parser.add_argument("--device",       default="auto")
    parser.add_argument("--num-workers",  type=int,   default=0,
                        help="DataLoader workers (0 for single-threaded, recommended on Windows)")
    parser.add_argument("--no-amp",       action="store_true",
                        help="Disable automatic mixed precision")
    parser.add_argument("--target-wdl-source", choices=["cp", "stored"], default="cp",
                        help="Training target for WDL-space loss: 'cp' derives sigmoid(score/600); 'stored' trusts the dataset wdl field")
    parser.add_argument("--loss-mode", choices=["wdl", "cp", "hybrid"], default="wdl",
                        help="Training objective: WDL-space, direct centipawn regression, or hybrid cp+WDL")
    parser.add_argument("--cp-beta", type=float, default=100.0,
                        help="Huber transition point in centipawns for cp/hybrid loss modes")
    parser.add_argument("--cp-scale", type=float, default=100.0,
                        help="Normalization divisor for cp/hybrid losses")
    parser.add_argument("--wdl-aux-weight", type=float, default=0.25,
                        help="Auxiliary WDL BCE weight for hybrid loss mode")
    parser.add_argument("--lr-schedule", choices=["cosine", "cosine_wr", "constant"], default="cosine",
                        help="LR schedule: 'cosine' = one-shot decay with optional warmup (recommended); "
                             "'cosine_wr' = warm-restart (legacy, avoids use); 'constant' = fixed LR")
    parser.add_argument("--warmup-epochs", type=int, default=5,
                        help="Linear warmup epochs before cosine decay starts (0 to disable)")
    parser.add_argument("--val-seed", type=int, default=42,
                        help="Seed for chunk-randomised train/val split to break file-order bias. "
                             "Pass -1 to use the legacy sequential (last 10%%) split.")
    parser.add_argument("--train-score-cap", type=int, default=0,
                        help="Cap |score| at this value (cp) at training time — no re-prep needed. "
                             "Recommended: 1200. Removes mop-up positions that saturate sigmoid "
                             "and dominate gradient with near-zero learning signal.")
    parser.add_argument("--bucket-adaptive-wdl-scale", action="store_true",
                        help="Use per-bucket WDL sigmoid scale (small for endgame, large for opening). "
                             "See nnue/loss.py BUCKET_WDL_SCALES for the table.")
    parser.add_argument("--color-flip-aug", action="store_true",
                        help="Apply color-flip augmentation during training (50%% of batches): "
                             "swap white/black features, negate score, invert WDL. "
                             "Effectively doubles unique positions seen per epoch.")
    parser.add_argument("--bucket-curriculum", type=int, default=0, metavar='EPOCHS',
                        help="Linearly decay per-bucket sample weights over this many epochs. "
                             "Combined with --bucket-weights to weight early-game buckets higher "
                             "during initial training. 0 = disabled.")
    parser.add_argument("--bucket-weights", type=str, default=None,
                        help="Comma-separated per-bucket weights for WeightedRandomSampler "
                             "(8 values, e.g. '5,4,3,2,1,1,1,1'). "
                             "Used alone for fixed weighting or with --bucket-curriculum for decay.")
    parser.add_argument("--ft-lr-scale", type=float, default=0.1,
                        help="LR multiplier for the Feature Transformer relative to output layers. "
                             "FT is 62M sparse params — it needs a lower LR than the output network. "
                             "Default 0.1 (FT LR = main LR * 0.1). Set 1.0 to disable.")
    parser.add_argument("--tensorboard", action="store_true",
                        help="Write TensorBoard logs to <checkpoint>/tb/ for live monitoring.")
    args = parser.parse_args()

    # ── Device ──
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    use_amp = (device.type == "cuda") and not args.no_amp
    use_pin = device.type == "cuda"
    print(f"Device: {device}  AMP: {use_amp}")
    print(f"Model variant: {args.model_variant}")
    print(f"Loss mode: {args.loss_mode}")
    print(f"Target WDL source: {args.target_wdl_source}")

    if device.type == "cuda":
        props = torch.cuda.get_device_properties(0)
        print(f"GPU: {props.name}  VRAM: {props.total_memory / 1024**3:.1f} GB")

    # ── Checkpoints ──
    ckpt_dir = Path(args.checkpoint)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    best_ckpt = ckpt_dir / "best.pt"
    log_path  = ckpt_dir / "training_log.csv"

    # ── Data ──
    print()
    data_paths = args.data  # list of one or more paths (nargs='+')
    if all(p.endswith('.bin') for p in data_paths):
        from .data import BinaryNNUEDataset, FastBinaryLoader, ChunkedBinaryLoader, InterleavedChunkedLoader, MultiChunkedBinaryLoader, _read_header
        _total_gb = sum(os.path.getsize(p) / 1024**3 for p in data_paths)
        _use_chunked = _total_gb > 2.0 and args.max_samples == 0

        if _use_chunked:
            if len(data_paths) == 1:
                print(f"  Dataset {_total_gb:.1f} GB — using interleaved loader (K=8, peak RAM ~4 GB)")
                _val_seed = None if args.val_seed == -1 else args.val_seed
                train_loader = InterleavedChunkedLoader(data_paths[0], args.batch_size,
                                                        chunk_gb=0.5, val_frac=0.1, is_val=False,
                                                        val_seed=_val_seed, interleave_k=8,
                                                        train_score_cap=args.train_score_cap)
                val_loader   = InterleavedChunkedLoader(data_paths[0], args.batch_size,
                                                        chunk_gb=0.5, val_frac=0.1, is_val=True,
                                                        val_seed=_val_seed, interleave_k=8,
                                                        train_score_cap=args.train_score_cap)
                train_sample_count = train_loader._n
                val_sample_count = val_loader._n
            else:
                sizes = [os.path.getsize(p)/1024**3 for p in data_paths]
                print(f"  {len(data_paths)} datasets [{', '.join(f'{s:.1f}GB' for s in sizes[:5])}{' ...' if len(sizes)>5 else ''}] "
                      f"= {_total_gb:.1f} GB total — using MultiChunkedBinaryLoader")
                train_loader = MultiChunkedBinaryLoader(data_paths, args.batch_size,
                                                        chunk_gb=0.5, val_frac=0.1, is_val=False,
                                                        train_score_cap=args.train_score_cap)
                val_loader   = MultiChunkedBinaryLoader(data_paths, args.batch_size,
                                                        chunk_gb=0.5, val_frac=0.1, is_val=True,
                                                        train_score_cap=args.train_score_cap)
                total_records = 0
                total_val_records = 0
                for p in data_paths:
                    meta = _read_header(p)
                    n = meta['n_records']
                    n_val = max(1, n // 10)
                    total_records += n
                    total_val_records += n_val
                val_sample_count = total_val_records
                train_sample_count = total_records - total_val_records
        else:
            # Small dataset(s) — preload into RAM
            data_path = data_paths[0]  # single-file path for small sets
            dataset   = BinaryNNUEDataset(data_path, max_samples=args.max_samples, preload=True)
            arr       = dataset._mmap
            if args.val_data:
                val_ds = BinaryNNUEDataset(args.val_data, preload=True)
                train_arr, val_arr = arr, val_ds._mmap
            else:
                n_val   = max(1, len(arr) // 10)
                n_train = len(arr) - n_val
                perm = np.random.permutation(len(arr))
                train_arr = arr[perm[:n_train]]
                val_arr   = arr[perm[n_train:]]
            train_loader = FastBinaryLoader(train_arr, args.batch_size, shuffle=True,
                                            train_score_cap=args.train_score_cap)
            val_loader   = FastBinaryLoader(val_arr,   args.batch_size, shuffle=False,
                                            train_score_cap=args.train_score_cap)
            train_sample_count = len(train_arr)
            val_sample_count = len(val_arr)
        batches_per_epoch = len(train_loader)
    else:
        import numpy as _np  # already imported as np, alias kept for clarity
        from .dataset import NNUEDataset, collate_fn as _collate
        dataset = NNUEDataset(data_paths[0], max_samples=args.max_samples, score_cap=args.score_cap)

        if args.val_data:
            val_dataset = NNUEDataset(args.val_data, score_cap=args.score_cap)
            train_dataset = dataset
        else:
            n_val = max(1, len(dataset) // 10)
            n_train = len(dataset) - n_val
            train_dataset, val_dataset = random_split(dataset, [n_train, n_val])

        train_loader = DataLoader(train_dataset, batch_size=args.batch_size,
                                  shuffle=True, collate_fn=_collate,
                                  num_workers=args.num_workers, pin_memory=False,
                                  persistent_workers=(args.num_workers > 0))
        val_loader = DataLoader(val_dataset, batch_size=args.batch_size,
                                shuffle=False, collate_fn=_collate,
                                num_workers=args.num_workers, pin_memory=False,
                                persistent_workers=(args.num_workers > 0))
        batches_per_epoch = math.ceil(len(train_dataset) / args.batch_size)
        train_sample_count = len(train_dataset)
        val_sample_count = len(val_dataset)

    val_batches = len(val_loader)
    if batches_per_epoch <= 0:
        raise ValueError(
            f"Training loader has zero batches for batch_size={args.batch_size:,} and "
            f"train_samples={train_sample_count:,}. Reduce --batch-size or increase samples."
        )
    if val_batches <= 0:
        raise ValueError(
            f"Validation loader has zero batches for batch_size={args.batch_size:,} and "
            f"val_samples={val_sample_count:,}. Reduce --batch-size, provide --val-data, or use more samples."
        )
    print(f"Train: {train_sample_count:,}  Val: {val_sample_count:,}  "
          f"Batch: {args.batch_size:,}  Train batches/epoch: {batches_per_epoch:,}  Val batches: {val_batches:,}")

    # ── Model ──
    model = build_model(args.model_variant).to(device)
    model._variant_name = args.model_variant
    if isinstance(model, NNUE):
        print(f"\n{model_summary(model)}\n")
    else:
        print(f"\nExperimental NNUE variant: {args.model_variant}")
        if hasattr(model, 'spec'):
            print(f"  FT={model.spec.ft_size}  hidden={model.spec.hidden_sizes}  activation={model.spec.activation}  ft_activation={model.spec.ft_activation}  buckets={OUTPUT_BUCKETS}")
        print(f"  Parameters: {model_param_count(model):,}\n")

    # ── Optimizer / Scheduler / Scaler ──
    # Split FT into its own param group at a lower LR.
    # The FT is 62M sparse embedding params; at the same LR as the 6.6M dense output
    # layers it takes over-large steps on rarely-seen features at peak LR.
    if isinstance(model, NNUE) and args.ft_lr_scale != 1.0:
        ft_params  = list(model.ft.parameters()) + [model.ft_bias]
        out_params = [p for n, p in model.named_parameters()
                      if not any(n.startswith(x) for x in ('ft.', 'ft_bias'))]
        param_groups = [
            {'params': ft_params,  'lr': args.lr * args.ft_lr_scale, 'weight_decay': args.wd},
            {'params': out_params, 'lr': args.lr,                     'weight_decay': args.wd},
        ]
        print(f"  Optimizer: FT lr={args.lr * args.ft_lr_scale:.2e}  output lr={args.lr:.2e}  wd={args.wd}")
    else:
        param_groups = model.parameters()
        print(f"  Optimizer: lr={args.lr:.2e}  wd={args.wd}")
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr, weight_decay=args.wd)

    _decay_epochs = max(1, args.epochs - args.warmup_epochs)
    if args.lr_schedule == 'cosine_wr':
        # Legacy warm-restart schedule — kept for ablation comparison only.
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            optimizer, T_0=20, T_mult=2, eta_min=1e-6
        )
    elif args.lr_schedule == 'constant':
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda _: 1.0)
    else:  # 'cosine' — one-shot decay, no restarts (default)
        if args.warmup_epochs > 0:
            _warmup = torch.optim.lr_scheduler.LinearLR(
                optimizer, start_factor=0.1, end_factor=1.0,
                total_iters=args.warmup_epochs,
            )
            _decay = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=_decay_epochs, eta_min=1e-6,
            )
            scheduler = torch.optim.lr_scheduler.SequentialLR(
                optimizer, schedulers=[_warmup, _decay],
                milestones=[args.warmup_epochs],
            )
        else:
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=args.epochs, eta_min=1e-6,
            )

    scaler = GradScaler() if use_amp else None

    # ── Resume ──
    start_epoch = 1
    best_val_mae = float('inf')
    best_epoch   = 0

    if args.resume and os.path.isfile(args.resume):
        print(f"Resuming from {args.resume}")
        ckpt = torch.load(args.resume, map_location=device)
        model.load_state_dict(ckpt['model_state_dict'])
        # Only restore optimizer state when param-group count matches.
        # A mismatch means the optimizer structure changed (e.g. FT split added),
        # in which case we start with fresh Adam moments but keep model weights + LR schedule.
        ckpt_n_groups = len(ckpt['optimizer_state_dict']['param_groups'])
        if ckpt_n_groups == len(optimizer.param_groups):
            optimizer.load_state_dict(ckpt['optimizer_state_dict'])
            # Re-apply CLI hyperparams — load_state_dict restores checkpoint's param_groups
            # (including lr and weight_decay), which would silently override --lr / --wd.
            for i, pg in enumerate(optimizer.param_groups):
                base_lr = args.lr * args.ft_lr_scale if (i == 0 and len(optimizer.param_groups) > 1) else args.lr
                pg['lr']           = base_lr
                pg['initial_lr']   = base_lr
                pg['weight_decay'] = args.wd
        else:
            print(f"  NOTE: optimizer param groups changed ({ckpt_n_groups} → {len(optimizer.param_groups)}) "
                  f"— skipping optimizer state, fresh Adam moments from epoch {ckpt['epoch']} weights.")
        if ckpt.get('scheduler_state_dict') and scheduler:
            scheduler.load_state_dict(ckpt['scheduler_state_dict'])
        if ckpt.get('scaler_state_dict') and scaler:
            scaler.load_state_dict(ckpt['scaler_state_dict'])
        start_epoch = ckpt['epoch'] + 1
        best_val_mae = ckpt.get('val_mae', float('inf'))
        best_epoch = ckpt['epoch']
        print(f"  Resuming from {args.resume}  (epoch {ckpt['epoch']}  best mae={best_val_mae:.2f}cp)")

    # ── TensorBoard ──
    tb_writer = None
    if args.tensorboard:
        if _TB_AVAILABLE:
            tb_dir = ckpt_dir / 'tb'
            tb_writer = _SummaryWriter(str(tb_dir))
            print(f"  TensorBoard: {tb_dir}  (tensorboard --logdir {tb_dir})")
        else:
            print("  TensorBoard: torch.utils.tensorboard not available — pip install tensorboard")

    # ── CSV log ──
    log_exists = log_path.exists()
    log_file = open(log_path, 'a', newline='')
    log_writer = csv_mod.writer(log_file)
    if not log_exists:
        log_writer.writerow(['epoch', 'train_loss', 'val_loss', 'val_mae', 'val_mse', 'val_bce', 'lr', 'elapsed_s'])

    # ── Bucket curriculum / WeightedRandomSampler ──────────────────────────
    # Parse --bucket-weights into a float array (8 values, one per bucket).
    # These are used to build a WeightedRandomSampler for the FastBinaryLoader
    # or ChunkedBinaryLoader paths.  Decays linearly toward all-1s over
    # --bucket-curriculum epochs.
    _base_bucket_weights = None
    if args.bucket_weights:
        try:
            _bw = [float(x.strip()) for x in args.bucket_weights.split(',')]
            if len(_bw) != OUTPUT_BUCKETS:
                raise ValueError(f'Expected {OUTPUT_BUCKETS} values, got {len(_bw)}')
            _base_bucket_weights = np.array(_bw, dtype=np.float32)
        except Exception as e:
            raise SystemExit(f'--bucket-weights parse error: {e}')
    elif args.bucket_curriculum > 0:
        # Default weights for curriculum: endgame-heavy start
        _base_bucket_weights = np.array([5., 4., 3., 2., 1., 1., 1., 1.], dtype=np.float32)

    def _epoch_bucket_weights(epoch: int) -> np.ndarray | None:
        """Return per-bucket weights for this epoch, applying linear curriculum decay."""
        if _base_bucket_weights is None:
            return None
        if args.bucket_curriculum <= 0:
            return _base_bucket_weights
        # Linearly interpolate: epoch 1 → base weights; epoch > curriculum → all-1s
        t = min(1.0, (epoch - 1) / max(args.bucket_curriculum, 1))
        weights = _base_bucket_weights * (1.0 - t) + np.ones(OUTPUT_BUCKETS, dtype=np.float32) * t
        return weights

    # ── Color-flip RNG ──────────────────────────────────────────────────────
    _color_flip_rng = np.random.default_rng(0xDEADBEEF) if args.color_flip_aug else None

    # ── Training loop ──
    _HD = (f"  {'Ep':>5} │ {'Train Loss':>13} │ {'Val Loss':>13} │ "
           f"{'MAE':>7} │ {'RMSE':>9} │ {'BCE':>8} │ {'LR':>9} │ {'Time':>7}")
    _SEP = '  ' + '─' * 6 + '┼' + '─' * 15 + '┼' + '─' * 15 + '┼' + '─' * 9 + '┼' + '─' * 11 + '┼' + '─' * 10 + '┼' + '─' * 11 + '┼' + '─' * 8

    def _print_header():
        print()
        print(_HD)
        print(_SEP)

    _print_header()

    try:
        for epoch in range(start_epoch, args.epochs + 1):
            t0 = time.time()

            # Apply bucket curriculum: update per-record sampling weights each epoch.
            # Only works for FastBinaryLoader (preloaded small datasets); chunked loaders
            # can't be reweighted per-epoch — use bucket_rebalance.py for those instead.
            if _base_bucket_weights is not None:
                bw = _epoch_bucket_weights(epoch)
                if hasattr(train_loader, 'set_bucket_weights'):
                    train_loader.set_bucket_weights(bw)
                    if epoch == start_epoch or (epoch - 1) % 10 == 0:
                        t_str = ', '.join(f'{w:.2f}' for w in bw)
                        print(f'        [curriculum] epoch {epoch}  bucket weights: [{t_str}]')
                elif epoch == start_epoch:
                    print('        [curriculum] NOTE: loader does not support weighted sampling '
                          '(large-dataset path). Use bucket_rebalance.py instead.')

            train_loss = train_epoch(model, train_loader, optimizer, scaler,
                                     device, args.lambda_loss, args.max_grad_norm,
                                     n_total_batches=batches_per_epoch,
                                     target_wdl_source=args.target_wdl_source,
                                     loss_mode=args.loss_mode,
                                     cp_beta=args.cp_beta,
                                     cp_scale=args.cp_scale,
                                     wdl_aux_weight=args.wdl_aux_weight,
                                     wdl_scale=BUCKET_WDL_SCALES if args.bucket_adaptive_wdl_scale else None,
                                     color_flip_aug=args.color_flip_aug,
                                     color_flip_rng=_color_flip_rng)
            val_loss, val_mae, val_mse, val_bce = validate(
                model, val_loader, device, args.lambda_loss,
                target_wdl_source=args.target_wdl_source,
                loss_mode=args.loss_mode,
                cp_beta=args.cp_beta,
                cp_scale=args.cp_scale,
                wdl_aux_weight=args.wdl_aux_weight,
                wdl_scale=BUCKET_WDL_SCALES if args.bucket_adaptive_wdl_scale else None,
            )

            lr = optimizer.param_groups[0]['lr']  # LR used during this epoch (log before stepping)
            scheduler.step()
            elapsed = time.time() - t0

            # ── Per-layer gradient norms (logged after backward, before optimizer step) ──
            # Compute after the last train_epoch call — grads are still attached.
            if tb_writer is not None:
                ft_gnorm  = torch.cat([p.grad.flatten() for p in (list(model.ft.parameters()) + [model.ft_bias])
                                       if p.grad is not None]).norm().item() if isinstance(model, NNUE) else 0.0
                out_gnorm = torch.cat([p.grad.flatten() for n, p in model.named_parameters()
                                       if p.grad is not None and not n.startswith(('ft.', 'ft_bias'))]
                                      ).norm().item() if isinstance(model, NNUE) else 0.0
                ft_wnorm  = model.ft.weight[:INPUT_SIZE].norm().item() if isinstance(model, NNUE) else 0.0
                tb_writer.add_scalars('loss',      {'train': train_loss, 'val': val_loss}, epoch)
                tb_writer.add_scalars('metrics',   {'mae': val_mae, 'rmse': val_mse**0.5}, epoch)
                tb_writer.add_scalars('grad_norm', {'ft': ft_gnorm, 'output': out_gnorm}, epoch)
                tb_writer.add_scalar('weight_norm/ft', ft_wnorm, epoch)
                tb_writer.add_scalars('lr',        {'ft': optimizer.param_groups[0]['lr'],
                                                    'output': optimizer.param_groups[-1]['lr']}, epoch)
                tb_writer.flush()

            rmse = val_mse ** 0.5
            best_marker = ' ★' if val_mae < best_val_mae else '  '
            print(f"  {epoch:>5d} │ {train_loss:>13,.5f} │ {val_loss:>13,.5f} │ "
                  f"{val_mae:>7.1f} │ {rmse:>9,.1f} │ {val_bce:>8.5f} │ {lr:>9.6f} │ {elapsed:>6.0f}s"
                  f"{best_marker}", flush=True)

            # ── Divergence analysis (inline, printed every epoch) ──
            _diag_window = 5  # epochs to look back for trend
            _gap = val_loss - train_loss
            _gap_pct = 100.0 * _gap / max(train_loss, 1e-9)
            log_writer.writerow([epoch, f"{train_loss:.6f}", f"{val_loss:.6f}",
                                 f"{val_mae:.2f}", f"{val_mse:.2f}", f"{val_bce:.6f}",
                                 f"{lr:.8f}", f"{elapsed:.1f}"])
            log_file.flush()

            # Read back last N rows from the CSV to compute trend
            _history = []
            try:
                import csv as _csv
                with open(log_path, 'r') as _f:
                    _rows = list(_csv.DictReader(_f))
                _history = [(float(r['train_loss']), float(r['val_loss']), float(r['val_mae']))
                            for r in _rows[-_diag_window:]]
            except Exception:
                pass

            _diag_parts = []
            if len(_history) >= 3:
                _gaps = [v - t for t, v, _ in _history]
                _gap_trend = _gaps[-1] - _gaps[0]          # positive = widening
                _mae_trend = _history[-1][2] - _history[0][2]  # positive = worsening

                if _gap_pct > 20 and _gap_trend > 0.005:
                    _diag_parts.append(f"\033[33m  ⚠  diverging: gap={_gap:.4f} (+{_gap_trend:.4f} over {len(_history)} ep) — consider lower LR or dropout\033[0m")
                elif _gap_pct > 10:
                    _diag_parts.append(f"     gap {_gap:.4f} ({_gap_pct:.1f}%)  trend={'▲' if _gap_trend > 0 else '▼'}{abs(_gap_trend):.4f}")

                if _mae_trend > 5 and epoch - best_epoch > 10:
                    _diag_parts.append(f"\033[33m  ⚠  MAE worsening: +{_mae_trend:.1f}cp over {len(_history)} ep  (best was ep{best_epoch})\033[0m")

            for _d in _diag_parts:
                print(_d, flush=True)

            # ── Best checkpoint (tracked by MAE — the interpretable metric) ──
            is_best = val_mae < best_val_mae
            if is_best:
                best_val_mae = val_mae
                best_epoch = epoch
                _save_checkpoint(str(best_ckpt), model, optimizer, scheduler,
                                scaler, epoch, val_loss, val_mae)
                print(f"        └─ new best  mae={val_mae:.2f}cp  rmse={val_mse**0.5:,.1f}  val_loss={val_loss:,.2f}")
                if args.export_best and isinstance(model, NNUE):
                    from .export import export
                    export(str(best_ckpt), args.export_path, verbose=False)
                    print(f"        └─ exported → {args.export_path}")
                elif args.export_best and not isinstance(model, NNUE):
                    print("        └─ skipped export (experimental variant is not engine-export compatible)")

            # ── Periodic snapshot ──
            if args.save_every > 0 and epoch % args.save_every == 0:
                snap = ckpt_dir / f"epoch_{epoch:04d}.pt"
                _save_checkpoint(str(snap), model, optimizer, scheduler,
                                scaler, epoch, val_loss, val_mae)

            # Reprint header every 20 epochs so it stays visible in long runs
            if epoch % 20 == 0:
                _print_header()

            # ── Early stopping ──
            if args.patience > 0 and epoch - best_epoch >= args.patience:
                print(f"\n  Early stop at epoch {epoch} — no MAE improvement for {args.patience} epochs")
                break

    finally:
        log_file.close()
        if tb_writer is not None:
            tb_writer.close()

    print(f"\nDone.  Best val_mae={best_val_mae:.2f}cp at epoch {best_epoch}")
    print(f"  Best: {best_ckpt}")
    print(f"  Log:  {log_path}")


if __name__ == "__main__":
    main()
