"""
tools/train_watch.py — Live training monitor for NNUE training runs.

Reads the CSV log written by ml/trainer.py every epoch and prints a rolling
analysis: divergence gap, MAE trend, gradient ratios (from TensorBoard events
if available), and actionable suggestions.

Usage:
    python tools/train_watch.py                          # auto-finds latest log
    python tools/train_watch.py ml/checkpoints_v8        # explicit checkpoint dir
    python tools/train_watch.py --interval 30            # poll every 30s (default: 15)
    python tools/train_watch.py --no-tb                  # skip TensorBoard event parsing
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

# ANSI colours (stripped on Windows if colorama not installed)
try:
    import colorama; colorama.init()
    _C = True
except ImportError:
    _C = False

def _col(code, text):
    return f"\033[{code}m{text}\033[0m" if _C or sys.platform != 'win32' else text

RED    = lambda t: _col('31;1', t)
YELLOW = lambda t: _col('33', t)
GREEN  = lambda t: _col('32', t)
CYAN   = lambda t: _col('36', t)
GREY   = lambda t: _col('90', t)
BOLD   = lambda t: _col('1', t)

# ── TensorBoard event reader (optional) ──────────────────────────────────

def _read_tb_scalars(tb_dir: Path, tag: str, last_n: int = 10):
    """Read the last N values of a scalar tag from TensorBoard event files.
    Returns list of (step, value) or empty list if unavailable.
    """
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        ea = EventAccumulator(str(tb_dir), size_guidance={'scalars': last_n * 2})
        ea.Reload()
        tags = ea.Tags().get('scalars', [])
        if tag not in tags:
            return []
        return [(e.step, e.value) for e in ea.Scalars(tag)[-last_n:]]
    except Exception:
        return []


# ── CSV helpers ───────────────────────────────────────────────────────────

def _read_csv(log_path: Path):
    """Return list of row dicts from the training CSV. Skips malformed rows."""
    rows = []
    try:
        with open(log_path, 'r', newline='') as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        'epoch':      int(row['epoch']),
                        'train_loss': float(row['train_loss']),
                        'val_loss':   float(row['val_loss']),
                        'val_mae':    float(row['val_mae']),
                        'val_mse':    float(row['val_mse']),
                        'val_bce':    float(row['val_bce']),
                        'lr':         float(row['lr']),
                        'elapsed_s':  float(row['elapsed_s']),
                    })
                except (KeyError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return rows


# ── Analysis ─────────────────────────────────────────────────────────────

def _analyse(rows, tb_dir: Path = None, use_tb: bool = True):
    """Compute and print analysis for the current training state."""
    if not rows:
        print(GREY("  No data yet — waiting for epoch 1 to complete..."))
        return

    latest = rows[-1]
    ep      = latest['epoch']
    train   = latest['train_loss']
    val     = latest['val_loss']
    mae     = latest['val_mae']
    lr      = latest['lr']
    elapsed = latest['elapsed_s']

    gap      = val - train
    gap_pct  = 100.0 * gap / max(train, 1e-9)
    best_row = min(rows, key=lambda r: r['val_mae'])
    best_ep  = best_row['epoch']
    best_mae = best_row['val_mae']
    stale    = ep - best_ep

    # ── Rolling window metrics ──
    W = min(10, len(rows))
    window = rows[-W:]
    gaps   = [r['val_loss'] - r['train_loss'] for r in window]
    maes   = [r['val_mae'] for r in window]
    gap_delta = gaps[-1] - gaps[0]    # + = widening
    mae_delta = maes[-1] - maes[0]    # + = worsening

    # ── TensorBoard grad norms ──
    ft_gnorm = out_gnorm = None
    if use_tb and tb_dir and tb_dir.exists():
        ft_data  = _read_tb_scalars(tb_dir, 'grad_norm/ft',     last_n=5)
        out_data = _read_tb_scalars(tb_dir, 'grad_norm/output', last_n=5)
        if ft_data:  ft_gnorm  = ft_data[-1][1]
        if out_data: out_gnorm = out_data[-1][1]

    # ── Print ──
    os.system('cls' if os.name == 'nt' else 'clear')
    print(BOLD("══ NNUE Training Watch ══════════════════════════════════════"))
    print(f"  Epoch        {BOLD(str(ep))}")
    print(f"  Train loss   {train:.5f}")
    print(f"  Val loss     {val:.5f}   gap={gap:+.5f} ({gap_pct:+.1f}%)")
    print(f"  Val MAE      {mae:.1f} cp   (best {best_mae:.1f}cp @ ep{best_ep}, stale={stale})")
    print(f"  LR           {lr:.2e}")
    print(f"  Epoch time   {elapsed:.0f}s")
    print()

    # ── Gradient norms ──
    if ft_gnorm is not None and out_gnorm is not None:
        ratio = ft_gnorm / max(out_gnorm, 1e-9)
        ratio_str = f"{ratio:.1f}×"
        ratio_col = RED(ratio_str) if ratio > 5 else (YELLOW(ratio_str) if ratio > 2 else GREEN(ratio_str))
        print(f"  Grad norms   FT={ft_gnorm:.3f}  output={out_gnorm:.3f}  ratio={ratio_col}")
    elif use_tb and tb_dir:
        print(GREY("  Grad norms   (not yet available — waiting for TB flush)"))

    # ── Trend analysis ──
    print()
    print(BOLD("── Trend (last %d epochs) ──" % W))

    # Gap trend
    if len(window) >= 3:
        trend_sym = '▲' if gap_delta > 0.001 else ('▼' if gap_delta < -0.001 else '─')
        trend_col = RED if gap_delta > 0.005 else (YELLOW if gap_delta > 0.001 else GREEN)
        print(f"  Val/train gap  {trend_col(f'{trend_sym} {gap_delta:+.4f}')}  over {W} epochs")

        mae_sym = '▲' if mae_delta > 1 else ('▼' if mae_delta < -1 else '─')
        mae_col = RED if mae_delta > 5 else (YELLOW if mae_delta > 1 else GREEN)
        print(f"  MAE trend      {mae_col(f'{mae_sym} {mae_delta:+.1f}cp')}  over {W} epochs")

    # ── Suggestions ──
    suggestions = []

    if gap_pct > 20 and gap_delta > 0.005:
        suggestions.append(RED("  ✗ Active divergence: val loss rising faster than train loss"))
        suggestions.append("    → Check grad_norm/ft in TensorBoard — if FT dominates, --ft-lr-scale 0.05")
        suggestions.append("    → Try --wd 5e-3 or add dropout to output layers")

    elif gap_pct > 10 and gap_delta > 0.002:
        suggestions.append(YELLOW("  ⚠ Mild divergence building — monitor for 5 more epochs"))

    elif gap_pct < 5 and mae_delta < 0:
        suggestions.append(GREEN("  ✓ Healthy: gap tight, MAE improving"))

    elif gap_pct < 5 and abs(mae_delta) < 1 and stale > 15:
        suggestions.append(YELLOW("  ⚠ Plateau: MAE flat for %d epochs" % stale))
        suggestions.append("    → LR may be too low for this stage. Check cosine schedule position.")

    if stale > 30:
        suggestions.append(RED(f"  ✗ No improvement for {stale} epochs — consider early stop"))
        suggestions.append("    → Run: .\\make.ps1 train -Resume ml/checkpoints_v8/best.pt")

    if suggestions:
        print()
        print(BOLD("── Suggestions ──"))
        for s in suggestions:
            print(s)

    # ── Epoch history table (last 15) ──
    print()
    print(BOLD("── History ──"))
    print(GREY("  ep   train     val       mae    gap%"))
    for r in rows[-15:]:
        g = r['val_loss'] - r['train_loss']
        gp = 100 * g / max(r['train_loss'], 1e-9)
        marker = ' ★' if r['epoch'] == best_ep else '  '
        row_col = RED if gp > 15 else (YELLOW if gp > 8 else (lambda x: x))
        print(row_col(f"  {r['epoch']:3d}  {r['train_loss']:.5f}  {r['val_loss']:.5f}  "
                       f"{r['val_mae']:6.1f}  {gp:+5.1f}%{marker}"))

    print()
    print(GREY(f"  Updated {time.strftime('%H:%M:%S')} — Ctrl+C to exit"))


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Live NNUE training monitor")
    parser.add_argument("checkpoint_dir", nargs='?', default=None,
                        help="Checkpoint directory (default: auto-find latest ml/checkpoints_*/)")
    parser.add_argument("--interval", type=float, default=15,
                        help="Poll interval in seconds (default: 15)")
    parser.add_argument("--no-tb", action="store_true",
                        help="Skip TensorBoard event parsing")
    parser.add_argument("--once", action="store_true",
                        help="Print once and exit (no polling loop)")
    args = parser.parse_args()

    # Auto-find checkpoint dir
    if args.checkpoint_dir:
        ckpt_dir = Path(args.checkpoint_dir)
    else:
        # Find most recently modified checkpoints_* dir
        base = Path("ml")
        candidates = sorted(base.glob("checkpoints_*"),
                            key=lambda p: p.stat().st_mtime if p.exists() else 0,
                            reverse=True)
        if not candidates:
            print(RED("No checkpoint directories found under ml/"))
            sys.exit(1)
        ckpt_dir = candidates[0]
        print(f"Auto-selected: {ckpt_dir}")

    log_path = ckpt_dir / "training_log.csv"
    tb_dir   = ckpt_dir / "tb"

    if args.once:
        _analyse(_read_csv(log_path), tb_dir=tb_dir, use_tb=not args.no_tb)
        return

    print(f"Watching {log_path}  (Ctrl+C to exit)")
    last_epoch = -1
    try:
        while True:
            rows = _read_csv(log_path)
            current_epoch = rows[-1]['epoch'] if rows else -1
            if current_epoch != last_epoch:
                _analyse(rows, tb_dir=tb_dir, use_tb=not args.no_tb)
                last_epoch = current_epoch
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
