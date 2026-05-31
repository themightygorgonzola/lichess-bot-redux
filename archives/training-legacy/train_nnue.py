"""
train_nnue.py â€” Main training script for the NNUE evaluation network.

Usage:
  python -m training.train_nnue --data training_data.csv [options]

Key options:
  --data PATH           Path to CSV training data (fen,score_cp)
  --val-data PATH       Validation CSV (optional; 10% auto-split if omitted)
  --epochs N            Epochs to train (default: 60)
  --batch-size N        Batch size (default: 16384)
  --lr FLOAT            Initial learning rate (default: 0.001)
  --wd FLOAT            Weight decay (default: 1e-5)
  --score-cap N         Drop positions with |score| > N cp (default: 3000)
  --checkpoint DIR      Directory for checkpoints (default: training/checkpoints)
  --save-every N        Also save periodic snapshot every N epochs (default: 5)
  --export-best         Auto-export nn.bin whenever best model improves
  --export-path PATH    Where to write nn.bin (default: nn.bin at repo root)
  --resume PATH         Resume from a checkpoint .pt file
  --patience N          Early-stop after N epochs without improvement (default: 20)
  --max-samples N       Limit training samples (0 = all)
  --device DEVICE       cuda / cpu (default: auto)
"""

import argparse
import csv as csv_mod
import os
import sys
import time
import math
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, random_split

from .data_loader import NNUEDataset, collate_fn
from .model import NNUE, count_parameters


# ---------------------------------------------------------------------------
# Loss
# ---------------------------------------------------------------------------

def sigmoid_loss(pred: torch.Tensor, target: torch.Tensor,
                 lambda_: float = 1.0, scale: float = 400.0) -> torch.Tensor:
    """
    Blended loss:
      lambda * MSE(pred, target)  +  (1-lambda) * BCE(sigmoid(pred/s), sigmoid(target/s))

    Pure MSE (lambda=1.0) works fine for centipawn regression;
    blending in the WDL cross-entropy term (lambda<1) can help the network
    model win-probability more accurately.
    """
    mse = torch.mean((pred - target) ** 2)
    if lambda_ >= 1.0:
        return mse
    pred_wp   = torch.sigmoid(pred   / scale)
    target_wp = torch.sigmoid(target / scale)
    bce = nn.functional.binary_cross_entropy(pred_wp, target_wp, reduction='mean')
    return lambda_ * mse + (1.0 - lambda_) * bce


# ---------------------------------------------------------------------------
# Train / eval loops
# ---------------------------------------------------------------------------

def train_epoch(model, loader, optimizer, device, lambda_loss):
    model.train()
    total_loss = 0.0
    n_batches  = 0

    for white_input, black_input, stm, scores in loader:
        white_input = white_input.to(device, non_blocking=True)
        black_input = black_input.to(device, non_blocking=True)
        stm         = stm.to(device, non_blocking=True)
        scores      = scores.to(device, non_blocking=True)

        optimizer.zero_grad()
        pred = model(white_input, black_input, stm).squeeze(1)
        loss = sigmoid_loss(pred, scores, lambda_=lambda_loss)
        loss.backward()
        optimizer.step()

        total_loss += loss.item()
        n_batches  += 1

    return total_loss / max(n_batches, 1)


@torch.no_grad()
def validate(model, loader, device, lambda_loss):
    model.eval()
    total_loss = 0.0
    total_mae  = 0.0
    n_batches  = 0
    n_samples  = 0

    for white_input, black_input, stm, scores in loader:
        white_input = white_input.to(device, non_blocking=True)
        black_input = black_input.to(device, non_blocking=True)
        stm         = stm.to(device, non_blocking=True)
        scores      = scores.to(device, non_blocking=True)

        pred = model(white_input, black_input, stm).squeeze(1)
        loss = sigmoid_loss(pred, scores, lambda_=lambda_loss)

        total_loss += loss.item()
        total_mae  += torch.abs(pred - scores).sum().item()
        n_batches  += 1
        n_samples  += scores.size(0)

    return total_loss / max(n_batches, 1), total_mae / max(n_samples, 1)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _save_checkpoint(path, model, optimizer, epoch, val_loss, val_mae):
    torch.save({
        'epoch':                epoch,
        'model_state_dict':     model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'val_loss':             val_loss,
        'val_mae':              val_mae,
    }, path)


def _export_nn_bin(checkpoint_path: str, output_path: str):
    """Re-use export_weights logic inline to avoid a subprocess call."""
    from .export_weights import export
    export(checkpoint_path, output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Train NNUE evaluation network")
    parser.add_argument("--data",         required=True,  help="Path to training CSV")
    parser.add_argument("--val-data",     default=None,   help="Path to validation CSV")
    parser.add_argument("--epochs",       type=int,   default=60)
    parser.add_argument("--batch-size",   type=int,   default=16384)
    parser.add_argument("--lr",           type=float, default=0.001)
    parser.add_argument("--wd",           type=float, default=1e-5)
    parser.add_argument("--lambda-loss",  type=float, default=1.0,
                        help="MSE vs BCE blend (1.0 = pure MSE, 0.7 recommended for WDL)")
    parser.add_argument("--score-cap",    type=int,   default=3000,
                        help="Drop positions with |score| > N cp (0 = keep all)")
    parser.add_argument("--checkpoint",   default="training/checkpoints",
                        help="Directory to store checkpoints")
    parser.add_argument("--save-every",   type=int,   default=5,
                        help="Save a periodic epoch snapshot every N epochs")
    parser.add_argument("--export-best",  action="store_true",
                        help="Auto-export nn.bin whenever best model improves")
    parser.add_argument("--export-path",  default="nn.bin",
                        help="Output path for auto-exported nn.bin")
    parser.add_argument("--resume",       default=None,
                        help="Resume from checkpoint .pt file")
    parser.add_argument("--patience",     type=int,   default=20,
                        help="Early-stop patience (epochs without improvement)")
    parser.add_argument("--max-samples",  type=int,   default=0)
    parser.add_argument("--device",       default="auto")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Device
    # ------------------------------------------------------------------
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    use_pin_memory = device.type == "cuda"
    print(f"Using device: {device}")

    # ------------------------------------------------------------------
    # Checkpoint directory
    # ------------------------------------------------------------------
    ckpt_dir = Path(args.checkpoint)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    best_ckpt    = ckpt_dir / "best.pt"
    log_csv_path = ckpt_dir / "training_log.csv"

    # ------------------------------------------------------------------
    # Data â€” load with score cap
    # ------------------------------------------------------------------
    print(f"\nLoading data (score_cap={args.score_cap or 'none'})...")
    dataset = NNUEDataset(args.data,
                          max_samples=args.max_samples,
                          score_cap=args.score_cap)

    if args.val_data:
        val_dataset   = NNUEDataset(args.val_data, score_cap=args.score_cap)
        train_dataset = dataset
    else:
        n_val   = max(1, len(dataset) // 10)
        n_train = len(dataset) - n_val
        train_dataset, val_dataset = random_split(dataset, [n_train, n_val])

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size,
                              shuffle=True,  collate_fn=collate_fn,
                              num_workers=0, pin_memory=use_pin_memory)
    val_loader   = DataLoader(val_dataset,   batch_size=args.batch_size,
                              shuffle=False, collate_fn=collate_fn,
                              num_workers=0, pin_memory=use_pin_memory)

    batches_per_epoch = math.ceil(len(train_dataset) / args.batch_size)
    print(f"Train: {len(train_dataset):,}  Val: {len(val_dataset):,}  "
          f"Batch: {args.batch_size:,}  Batches/epoch: {batches_per_epoch}")

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------
    model = NNUE().to(device)
    print(f"Model parameters: {count_parameters(model):,}")

    # ------------------------------------------------------------------
    # Optimizer / scheduler
    # ------------------------------------------------------------------
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)
    # Cosine-annealing with warm restarts gives better coverage of the loss
    # landscape than ReduceLROnPlateau on long runs.
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        optimizer, T_0=10, T_mult=2, eta_min=1e-5
    )

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------
    start_epoch    = 1
    best_val_loss  = float('inf')
    best_epoch     = 0

    if args.resume and os.path.isfile(args.resume):
        print(f"Resuming from {args.resume}")
        ckpt = torch.load(args.resume, map_location=device)
        model.load_state_dict(ckpt['model_state_dict'])
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])
        start_epoch   = ckpt['epoch'] + 1
        best_val_loss = ckpt.get('val_loss', float('inf'))
        best_epoch    = ckpt['epoch']
        print(f"  Resumed at epoch {ckpt['epoch']}  val_loss={best_val_loss:.4f}")

    # ------------------------------------------------------------------
    # CSV log (append-safe so resume continues the same log)
    # ------------------------------------------------------------------
    log_exists = log_csv_path.exists()
    log_file   = open(log_csv_path, 'a', newline='')
    log_writer = csv_mod.writer(log_file)
    if not log_exists:
        log_writer.writerow(['epoch', 'train_loss', 'val_loss', 'val_mae', 'lr', 'elapsed_s'])

    # ------------------------------------------------------------------
    # Training loop
    # ------------------------------------------------------------------
    header = (f"{'Epoch':>5} {'Train Loss':>12} {'Val Loss':>12} "
              f"{'Val MAE':>10} {'LR':>10} {'Time':>8}")
    print(f"\n{header}")
    print("-" * 62)

    try:
        for epoch in range(start_epoch, args.epochs + 1):
            t0 = time.time()

            train_loss          = train_epoch(model, train_loader, optimizer, device, args.lambda_loss)
            val_loss, val_mae   = validate(model, val_loader, device, args.lambda_loss)

            scheduler.step(epoch - 1)          # CosineAnnealingWarmRestarts takes step count
            lr      = optimizer.param_groups[0]['lr']
            elapsed = time.time() - t0

            print(f"{epoch:5d} {train_loss:12.4f} {val_loss:12.4f} {val_mae:10.1f} "
                  f"{lr:10.6f} {elapsed:7.1f}s", flush=True)

            log_writer.writerow([epoch, f"{train_loss:.6f}", f"{val_loss:.6f}",
                                  f"{val_mae:.2f}", f"{lr:.8f}", f"{elapsed:.1f}"])
            log_file.flush()

            # -- Best checkpoint -------------------------------------------
            is_best = val_loss < best_val_loss
            if is_best:
                best_val_loss = val_loss
                best_epoch    = epoch
                _save_checkpoint(str(best_ckpt), model, optimizer, epoch, val_loss, val_mae)
                print(f"  -> [best]  saved {best_ckpt}  (val_loss={val_loss:.4f}  mae={val_mae:.1f}cp)")
                if args.export_best:
                    _export_nn_bin(str(best_ckpt), args.export_path)
                    print(f"  -> [best]  exported {args.export_path}")

            # -- Periodic snapshot -----------------------------------------
            if args.save_every > 0 and epoch % args.save_every == 0:
                snap_path = ckpt_dir / f"epoch_{epoch:04d}.pt"
                _save_checkpoint(str(snap_path), model, optimizer, epoch, val_loss, val_mae)
                print(f"  -> [snap]  saved {snap_path}")

            # -- Early stopping --------------------------------------------
            if epoch - best_epoch >= args.patience:
                print(f"\nEarly stopping at epoch {epoch} "
                      f"(no improvement for {args.patience} epochs; best was epoch {best_epoch})")
                break

    finally:
        log_file.close()

    print(f"\nTraining complete.")
    print(f"  Best val_loss = {best_val_loss:.4f}  at epoch {best_epoch}")
    print(f"  Best checkpoint: {best_ckpt}")
    print(f"  Training log:    {log_csv_path}")
    if not args.export_best:
        print(f"\nExport weights:")
        print(f"  python -m training.export_weights --checkpoint {best_ckpt} --output nn.bin")


if __name__ == "__main__":
    main()
