#!/usr/bin/env python3
"""
compare_design_benchmarks.py â€” Compare architecture design benchmark results.

This tool merges:
  - fit-capacity benchmark JSONs from tools/fit_binary_subset.py
  - engine search benchmark JSONs from tools/search_bench.py

into one ranked table / CSV / JSON summary.

Primary use:
  - maintain a manifest of candidate designs
  - attach each design's fit and search benchmark outputs
  - generate one comparison table across all designs

Manifest format:
  {
    "designs": [
      {
        "name": "wide_gelu_smoke",
        "fit_json": "results/arch_bench/wide_gelu_10000_0.json",
        "search_json": "results/search_bench/wide_gelu_core_d8.json",
        "notes": "example"
      }
    ]
  }
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_path(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = ROOT / p
    return p


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    if isinstance(data, dict):
        designs = data.get("designs", [])
    elif isinstance(data, list):
        designs = data
    else:
        raise ValueError("Manifest must be a list or object with a 'designs' field")
    if not isinstance(designs, list):
        raise ValueError("Manifest 'designs' must be a list")
    return [d for d in designs if isinstance(d, dict)]


def _load_fit_payload(design: dict[str, Any]) -> dict[str, Any] | None:
    path = _as_path(design.get("fit_json"))
    if not path or not path.is_file():
        return None
    data = _load_json(path)
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        wanted = design.get("fit_variant") or design.get("variant") or design.get("model_variant")
        if wanted:
            for row in data:
                if not isinstance(row, dict):
                    continue
                if row.get("variant") == wanted or row.get("model_variant") == wanted:
                    return row
        if len(data) == 1 and isinstance(data[0], dict):
            return data[0]
    return None


def _load_search_payload(design: dict[str, Any]) -> dict[str, Any] | None:
    path = _as_path(design.get("search_json"))
    if not path or not path.is_file():
        return None
    data = _load_json(path)
    return data if isinstance(data, dict) else None


def _load_pybench_payload(design: dict[str, Any]) -> dict[str, Any] | None:
    path = _as_path(design.get("pybench_json"))
    if not path or not path.is_file():
        return None
    data = _load_json(path)
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        wanted = design.get("variant") or design.get("model_variant")
        if wanted:
            for row in data:
                if isinstance(row, dict) and (row.get("variant") == wanted or row.get("model_variant") == wanted):
                    return row
        if len(data) == 1 and isinstance(data[0], dict):
            return data[0]
    return None


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2 == 1:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _balanced_score(row: dict[str, Any]) -> float:
    fit_success = 1.0 if row.get("fit_success") else 0.0
    fit_time = _safe_float(row.get("fit_time_to_target_s"), 0.0)
    best_mae = max(_safe_float(row.get("fit_best_mae"), 1e9), 1e-9)
    speed_metric = max(_safe_float(row.get("search_total_nps"), 0.0), _safe_float(row.get("py_best_nps_proxy"), 0.0))

    fit_term = 0.0
    if fit_success and fit_time > 0.0:
        fit_term = 1.0 / fit_time
    elif fit_success:
        fit_term = 1.0

    mae_term = 1.0 / (1.0 + math.log10(1.0 + best_mae * 1000.0)) if best_mae < 1e8 else 0.0
    nps_term = speed_metric / 1_000_000.0
    return fit_term * 5.0 + mae_term * 2.0 + nps_term


def _build_row(design: dict[str, Any]) -> dict[str, Any]:
    fit = _load_fit_payload(design)
    search = _load_search_payload(design)
    pybench = _load_pybench_payload(design)
    search_summary = search.get("summary", {}) if isinstance(search, dict) else {}
    search_config = search.get("config", {}) if isinstance(search, dict) else {}
    py_forward = pybench.get("forward_benchmarks", []) if isinstance(pybench, dict) else []
    py_forward_pps = [
        _safe_float(item.get("positions_per_sec"), 0.0)
        for item in py_forward
        if isinstance(item, dict)
    ]
    py_median_nps_proxy = _median([v for v in py_forward_pps if v > 0.0])

    row: dict[str, Any] = {
        "name": design.get("name", "unnamed"),
        "family": design.get("family", ""),
        "variant": design.get("variant", design.get("model_variant", "")),
        "status": design.get("status", ""),
        "notes": design.get("notes", ""),
        "fit_json": design.get("fit_json", ""),
        "pybench_json": design.get("pybench_json", ""),
        "search_json": design.get("search_json", ""),
        "fit_success": bool(fit.get("success")) if fit else False,
        "fit_pass_epoch": _safe_int(fit.get("pass_epoch"), -1) if fit else -1,
        "fit_best_epoch": _safe_int(fit.get("best_epoch"), -1) if fit else -1,
        "fit_best_mae": _safe_float(fit.get("best_mae"), 0.0) if fit else 0.0,
        "fit_final_mae": _safe_float(fit.get("final_mae"), 0.0) if fit else 0.0,
        "fit_time_to_target_s": _safe_float(fit.get("time_to_target_s", fit.get("elapsed_s")), 0.0) if fit else 0.0,
        "fit_stability_penalty": _safe_float(fit.get("stability_penalty"), 0.0) if fit else 0.0,
        "fit_instability_events": _safe_int(fit.get("instability_events"), 0) if fit else 0,
        "fit_post_best_rebound": _safe_float(fit.get("post_best_mae_rebound_ratio"), 1.0) if fit else 1.0,
        "fit_auc_log10_mae": _safe_float(fit.get("auc_log10_mae"), 0.0) if fit else 0.0,
        "param_count": (
            _safe_int(fit.get("param_count", pybench.get("param_count", design.get("param_count"))), 0)
            if fit else
            _safe_int((pybench.get("param_count", design.get("param_count")) if pybench else design.get("param_count")), 0)
        ),
        "search_positions": _safe_int(search_summary.get("positions"), 0),
        "search_total_nps": _safe_int(search_summary.get("total_nps"), 0),
        "search_median_nps": _safe_int(search_summary.get("median_nps"), 0),
        "search_min_nps": _safe_int(search_summary.get("min_nps"), 0),
        "search_max_nps": _safe_int(search_summary.get("max_nps"), 0),
        "search_depth": _safe_int(search_config.get("depth"), 0),
        "search_threads": _safe_int(search_config.get("threads"), 0),
        "py_best_nps_proxy": _safe_float(pybench.get("best_nps_proxy"), 0.0) if pybench else 0.0,
        "py_median_nps_proxy": float(py_median_nps_proxy),
        "py_best_forward_positions_per_sec": _safe_float(pybench.get("best_forward_positions_per_sec"), 0.0) if pybench else 0.0,
        "py_best_train_samples_per_sec": _safe_float(pybench.get("best_train_samples_per_sec"), 0.0) if pybench else 0.0,
        "py_best_forward_batch_size": _safe_int(pybench.get("best_forward_batch_size"), 0) if pybench else 0,
        "py_best_train_batch_size": _safe_int(pybench.get("best_train_batch_size"), 0) if pybench else 0,
    }
    speed_metric = max(_safe_float(row.get("search_total_nps"), 0.0), _safe_float(row.get("py_best_nps_proxy"), 0.0))
    row["nps_per_mparam"] = (speed_metric / (row["param_count"] / 1_000_000.0)) if row["param_count"] > 0 else 0.0
    row["effective_nps"] = int(speed_metric)
    row["effective_med_nps"] = int(max(_safe_float(row.get("search_median_nps"), 0.0), _safe_float(row.get("py_median_nps_proxy"), 0.0)))
    row["fit_pass_epoch_display"] = (str(row["fit_pass_epoch"]) if row["fit_pass_epoch"] and row["fit_pass_epoch"] > 0 else "--")
    row["balanced_score"] = _balanced_score(row)
    return row


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        ("name", 24),
        ("fit", 6),
        ("pass_ep", 8),
        ("best_mae", 10),
        ("NPS", 10),
        ("medNPS", 10),
        ("Mparams", 9),
        ("NPS/Mp", 10),
        ("score", 8),
    ]
    line = " ".join(f"{title:<{width}}" for title, width in headers)
    print(line)
    print("-" * len(line))
    for row in rows:
        nps_display = int(_safe_float(row.get("effective_nps"), 0.0))
        med_nps_display = row.get("effective_med_nps", 0)
        values = [
            f"{row['name']:<24}",
            f"{('yes' if row['fit_success'] else 'no'):<6}",
            f"{row['fit_pass_epoch_display']:<8}",
            f"{row['fit_best_mae']:<10.4f}",
            f"{nps_display:<10}",
            f"{med_nps_display:<10}",
            f"{(row['param_count'] / 1_000_000.0):<9.2f}",
            f"{row['nps_per_mparam']:<10.0f}",
            f"{row['balanced_score']:<8.3f}",
        ]
        print(" ".join(values))


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare architecture design benchmark outputs")
    ap.add_argument("--manifest", required=True, help="JSON manifest listing designs and benchmark files")
    ap.add_argument("--sort", choices=["balanced", "nps", "fit"], default="balanced")
    ap.add_argument("--output-json", default="", help="Optional summary JSON output path")
    ap.add_argument("--output-csv", default="", help="Optional summary CSV output path")
    args = ap.parse_args()

    manifest_path = _as_path(args.manifest)
    if not manifest_path or not manifest_path.is_file():
        raise SystemExit(f"Manifest not found: {args.manifest}")

    rows = [_build_row(d) for d in _load_manifest(manifest_path)]

    if args.sort == "nps":
        rows.sort(key=lambda r: (_safe_int(r.get("search_total_nps")), _safe_float(r.get("balanced_score"))), reverse=True)
    elif args.sort == "fit":
        rows.sort(key=lambda r: (
            1 if r.get("fit_success") else 0,
            -_safe_int(r.get("fit_pass_epoch"), -1),
            -1.0 / max(_safe_float(r.get("fit_best_mae"), 1e9), 1e-9),
        ), reverse=True)
    else:
        rows.sort(key=lambda r: _safe_float(r.get("balanced_score")), reverse=True)

    _print_table(rows)

    if args.output_json:
        out_json = _as_path(args.output_json)
        assert out_json is not None
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {out_json}")
    if args.output_csv:
        out_csv = _as_path(args.output_csv)
        assert out_csv is not None
        _write_csv(out_csv, rows)
        print(f"Wrote {out_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())