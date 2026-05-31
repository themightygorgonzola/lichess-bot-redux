#!/usr/bin/env python3
"""
run_design_sweep.py â€” Manifest-driven architecture sweep runner.

Goal:
  - Define a set of candidate designs once
  - Hit go
  - Run the same benchmark stages for each design
  - Record outputs in a consistent directory layout
  - Emit a filled comparison manifest at the end

Current supported stages:
  1. `fit10k`   â€” exact-fit benchmark on a fixed 10k subset
  2. `pyspeed`  â€” Python forward/train throughput benchmark on the same cached sample

Engine search benchmarks remain a separate stage because only exported/integrated
designs can run through the C++ engine benchmark path.

Manifest format:
{
  "globals": {
    "data": "data/processed/mean-alltime-dedup.bin",
    "sample_size": 10000,
    "sample_seed": 0,
    "device": "cuda",
    "target_mae": 0.1,
    "target_maxerr": 0.1,
    "max_epochs": 800,
    "print_every": 200,
    "speed_batch_sizes": [1024, 4096, 10000],
    "speed_warmup": 10,
    "speed_iters": 30,
    "train_speed_iters": 10
  },
  "designs": [
    {
      "name": "wide_gelu",
      "variant": "wide_gelu",
      "family": "gelu",
      "status": "candidate"
    }
  ]
}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_ROOT = ROOT / "results" / "design_sweeps"

FIT_SCRIPT = ROOT / "tools" / "fit_binary_subset.py"
BENCH_SCRIPT = ROOT / "tools" / "benchmark_fit_variants.py"


def _rooted(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = ROOT / p
    return p


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Sweep manifest must be a JSON object")
    data.setdefault("globals", {})
    data.setdefault("designs", [])
    if not isinstance(data["designs"], list):
        raise ValueError("Sweep manifest 'designs' must be a list")
    return data


def _run(cmd: list[str]) -> int:
    print("\n$", " ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(proc.returncode)


def _fit_cmd(globals_cfg: dict[str, Any], design: dict[str, Any], design_dir: Path, sample_cache: Path) -> tuple[list[str], Path]:
    out_json = design_dir / "fit_10000.json"
    variant = design["variant"]
    cmd = [
        sys.executable, str(FIT_SCRIPT),
        "--data", str(_rooted(globals_cfg.get("data", "data/processed/mean-alltime-dedup.bin"))),
        "--sample-size", str(int(globals_cfg.get("sample_size", 10000))),
        "--sample-seed", str(int(globals_cfg.get("sample_seed", 0))),
        "--sample-cache", str(sample_cache),
        "--device", str(globals_cfg.get("device", "cuda")),
        "--model-variant", str(variant),
        "--lr", str(globals_cfg.get("lr", design.get("lr", 0.01))),
        "--weight-decay", str(globals_cfg.get("weight_decay", design.get("weight_decay", 0.0))),
        "--batch-size", str(globals_cfg.get("batch_size", design.get("batch_size", 0))),
        "--max-epochs", str(globals_cfg.get("max_epochs", 800)),
        "--target-mae", str(globals_cfg.get("target_mae", 0.1)),
        "--target-maxerr", str(globals_cfg.get("target_maxerr", 0.1)),
        "--print-every", str(globals_cfg.get("print_every", 200)),
        "--history-mode", str(globals_cfg.get("history_mode", "full")),
        "--output-json", str(out_json),
    ]
    return cmd, out_json


def _speed_cmd(globals_cfg: dict[str, Any], design: dict[str, Any], design_dir: Path, sample_cache: Path) -> tuple[list[str], Path]:
    out_prefix = design_dir / "pybench"
    out_json = Path(str(out_prefix) + ".json")
    variant = design["variant"]
    speed_batch_sizes = globals_cfg.get("speed_batch_sizes", [1024, 4096, 10000])
    cmd = [
        sys.executable, str(BENCH_SCRIPT),
        "--data", str(_rooted(globals_cfg.get("data", "data/processed/mean-alltime-dedup.bin"))),
        "--sample-size", str(int(globals_cfg.get("sample_size", 10000))),
        "--sample-seed", str(int(globals_cfg.get("sample_seed", 0))),
        "--sample-cache", str(sample_cache),
        "--device", str(globals_cfg.get("device", "cuda")),
        "--variants", str(variant),
        "--skip-fit",
        "--speed-warmup", str(int(globals_cfg.get("speed_warmup", 10))),
        "--speed-iters", str(int(globals_cfg.get("speed_iters", 30))),
        "--train-speed-iters", str(int(globals_cfg.get("train_speed_iters", 10))),
        "--summary-prefix", str(out_prefix),
    ]
    if speed_batch_sizes:
        cmd += ["--speed-batch-sizes", *[str(int(x)) for x in speed_batch_sizes]]
    return cmd, out_json


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a manifest-driven architecture sweep")
    ap.add_argument("--manifest", required=True, help="Sweep manifest JSON")
    ap.add_argument("--out-dir", default="", help="Optional output folder override")
    ap.add_argument("--stages", nargs="+", default=["fit10k", "pyspeed"], choices=["fit10k", "pyspeed"])
    ap.add_argument("--continue-on-error", action="store_true")
    args = ap.parse_args()

    manifest_path = _rooted(args.manifest)
    if not manifest_path or not manifest_path.is_file():
        raise SystemExit(f"Sweep manifest not found: {args.manifest}")

    manifest = _load_manifest(manifest_path)
    globals_cfg: dict[str, Any] = manifest.get("globals", {})
    designs: list[dict[str, Any]] = manifest.get("designs", [])
    if not designs:
        raise SystemExit("Sweep manifest contains no designs")

    sweep_name = manifest_path.stem
    out_dir = _rooted(args.out_dir) if args.out_dir else (RESULTS_ROOT / sweep_name)
    assert out_dir is not None
    out_dir.mkdir(parents=True, exist_ok=True)

    sample_size = int(globals_cfg.get("sample_size", 10000))
    sample_seed = int(globals_cfg.get("sample_seed", 0))
    sample_cache = out_dir / f"sample_{sample_size}_{sample_seed}.npy"

    completed_designs: list[dict[str, Any]] = []

    for design in designs:
        name = design.get("name") or design.get("variant")
        if not name or not design.get("variant"):
            raise SystemExit(f"Invalid design entry: {design}")

        print(f"\n===== DESIGN: {name} =====")
        design_dir = out_dir / name
        design_dir.mkdir(parents=True, exist_ok=True)

        design_record = dict(design)
        design_record.setdefault("family", "")
        design_record.setdefault("status", "candidate")

        if "fit10k" in args.stages:
            cmd, fit_json = _fit_cmd(globals_cfg, design, design_dir, sample_cache)
            rc = _run(cmd)
            design_record["fit_json"] = str(fit_json.relative_to(ROOT)) if fit_json.is_file() else ""
            design_record["fit_exit_code"] = rc
            if rc != 0 and not args.continue_on_error:
                raise SystemExit(f"Fit stage failed for {name} with exit code {rc}")

        if "pyspeed" in args.stages:
            cmd, bench_json = _speed_cmd(globals_cfg, design, design_dir, sample_cache)
            rc = _run(cmd)
            design_record["pybench_json"] = str(bench_json.relative_to(ROOT)) if bench_json.is_file() else ""
            design_record["search_json"] = design_record.get("search_json", "")
            design_record["pyspeed_exit_code"] = rc
            if rc != 0 and not args.continue_on_error:
                raise SystemExit(f"Python speed stage failed for {name} with exit code {rc}")

        completed_designs.append(design_record)

    completed_manifest = {
        "source_manifest": str(manifest_path.relative_to(ROOT)),
        "globals": globals_cfg,
        "designs": completed_designs,
    }
    completed_manifest_path = out_dir / "completed_manifest.json"
    completed_manifest_path.write_text(json.dumps(completed_manifest, indent=2), encoding="utf-8")

    print(f"\nWrote {completed_manifest_path}")
    print("Next:")
    print(f"  python tools/compare_design_benchmarks.py --manifest {completed_manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())