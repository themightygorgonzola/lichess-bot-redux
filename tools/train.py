"""
tools/train.py — Training CLI wrapper for NNUE v2.

Usage examples:
  # Quick 5-epoch smoke test (100K positions, batch 4096)
  python tools/train.py --smoke

  # Full training on the consolidated deduplicated dataset
  python tools/train.py --data data/processed/mean-alltime-dedup.bin

  # Custom run with auto-export to engine
  python tools/train.py --data data/processed/mean-alltime-dedup.bin --epochs 20 --export-engine

  # Resume from checkpoint
  python tools/train.py --data data/processed/mean-alltime-dedup.bin --resume ml/checkpoints/best.pt

Smoke preset overrides:
  --epochs 5  --max-samples 100000  --batch-size 4096  --save-every 1  --export-best
"""

import argparse
import shutil
import sys
from pathlib import Path

# ── Resolve workspace root from this file's location ──────────────────────────
ROOT = Path(__file__).resolve().parent.parent
ENGINE_NN = ROOT / "bot" / "engine" / "nn.bin"


def build_args(raw: list[str]) -> list[str]:
    """
    Pre-process raw sys.argv (minus script name):
      • Expand --smoke into preset flags
      • Inject a default --data if none given
      • Strip --export-engine (handled locally)
    Returns a flat list suitable for trainer.main() via sys.argv.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--smoke",         action="store_true")
    p.add_argument("--export-engine", action="store_true")
    p.add_argument("--data",          default=None)
    known, rest = p.parse_known_args(raw)

    args: list[str] = []

    # ── Default data path ──────────────────────────────────────────────────────
    data_path = known.data or str(ROOT / "data" / "processed" / "mean-alltime-dedup.bin")
    args += ["--data", data_path]

    # ── Smoke preset ───────────────────────────────────────────────────────────
    if known.smoke:
        print("[train.py] --smoke: epochs=5  max-samples=100000  batch-size=4096  save-every=1  export-best")
        smoke_flags = {
            "--epochs":      "5",
            "--max-samples": "100000",
            "--batch-size":  "4096",
            "--save-every":  "1",
        }
        # Apply only if not already in rest
        for flag, val in smoke_flags.items():
            if flag not in rest:
                args += [flag, val]
        if "--export-best" not in rest:
            args.append("--export-best")

    args += rest
    return args, known.export_engine


def main():
    raw = sys.argv[1:]
    forwarded, export_engine = build_args(raw)

    # ── Run trainer ────────────────────────────────────────────────────────────
    sys.path.insert(0, str(ROOT))
    # Temporarily override sys.argv so trainer's argparse sees the right flags
    sys.argv = ["trainer"] + forwarded

    from ml.trainer import main as trainer_main
    trainer_main()

    # ── Deploy to engine ───────────────────────────────────────────────────────
    if export_engine:
        # Locate the exported bin (trainer writes to --export-path, default nn_v2.bin)
        ep_flag = None
        for i, a in enumerate(forwarded):
            if a == "--export-path" and i + 1 < len(forwarded):
                ep_flag = forwarded[i + 1]
        exported = Path(ep_flag) if ep_flag else ROOT / "nn_v2.bin"
        if not exported.is_absolute():
            exported = ROOT / exported

        if exported.exists():
            ENGINE_NN.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exported, ENGINE_NN)
            print(f"\n[train.py] Deployed {exported.name} -> {ENGINE_NN}")
        else:
            print(f"[train.py] WARNING: export-engine requested but {exported} not found.")


if __name__ == "__main__":
    main()
