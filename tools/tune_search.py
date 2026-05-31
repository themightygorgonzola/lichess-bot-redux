#!/usr/bin/env python3
"""
tools/tune_search.py — Redux search parameter tuner.

Runs coordinate-descent over search params, measuring total nodes at fixed
depth (fewer nodes = more effective pruning = more depth for same time budget).

Usage:
    python tools/tune_search.py [options]

Options:
    --engine PATH     path to engine binary (default: bot/engine/redux-hce.exe)
    --depth N         bench depth (default: 8)
    --threads N       bench threads (default: 1)
    --hash N          hash size in MB (default: 64)
    --iters N         coordinate-descent iterations (default: 5)
    --output FILE     save results JSON to file (default: results/tune_TIMESTAMP.json)
    --baseline-only   just run baseline bench and exit
    --params FILE     load param set from JSON file (skip descent, just bench it)
    --sf PATH         Stockfish (or any UCI engine) for tree-shape comparison
    --no-build        skip make.ps1 build step

Metric: total nodes at fixed depth (lower = better pruning efficiency).
Quality check: bestmoves must match baseline (± tolerance across positions).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import datetime
from copy import deepcopy
from pathlib import Path

# Force UTF-8 output on Windows (avoid cp1252 encoding errors with box-drawing chars)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


WORKSPACE = Path(__file__).parent.parent
ENGINE_DEFAULT = WORKSPACE / "bot" / "engine" / "redux-hce.exe"
SF_DEFAULT     = WORKSPACE / "engines" / "stockfish-17.1" / "stockfish" / "stockfish-windows-x86-64-avx2.exe"
RESULTS_DIR    = WORKSPACE / "results"

# ── Bench position suite — mirrors benchmark.cpp build_default_suite() ─────────
# Used for per-position SF tree comparison. Keep in sync with benchmark.cpp.
BENCH_POSITIONS = [
    # name                  FEN
    ("startpos",          "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ("kiwipete",          "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    ("audit_p1",          "4k2r/ppp2ppp/8/8/3qP3/8/PPP2PPP/4K2R w Kk - 0 1"),
    ("audit_p2",          "r3k3/ppp2ppp/8/2ppp3/3R4/8/PPP2PPP/4K3 w - - 0 1"),
    ("audit_p4",          "8/p4k2/3n4/3P4/8/8/P4K2/8 w - - 0 1"),
    ("audit_p6",          "r1bq1rk1/pp2ppbp/2pp1np1/8/2PPP3/2N2N2/PP2BPPP/R1BQ1RK1 b - - 0 9"),
    ("audit_p7",          "3r2k1/pp3ppp/8/8/8/8/PPPrrPPP/3R2K1 w - - 0 1"),
    ("audit_p8",          "2r3k1/5pp1/p2p1n1p/1p1P4/1P2P3/P1N2N1P/5PP1/2R3K1 w - - 0 1"),
    ("audit_p9",          "r1bq1rk1/pp3ppp/2n1pn2/2pp4/3P4/2PBPN2/PP3PPP/R1BQ1RK1 w - - 0 8"),
    ("audit_p10",         "6k1/5pp1/8/3r4/3R4/8/5PP1/6K1 w - - 0 1"),
    ("pos_complex_mg",    "r2q1rk1/pp2ppbp/2np1np1/8/3PP3/2N2N2/PP2BPPP/R1BQ1RK1 w - - 0 9"),
    ("pos_sicilian",      "r1bqkb1r/pp3ppp/2npbn2/4p3/3PP3/2N2N2/PPP2PPP/R1BQKB1R w KQkq - 0 7"),
    ("pos_rook_eg",       "5rk1/R4pp1/5n1p/8/8/5N1P/5PP1/5RK1 w - - 0 1"),
    ("pos_pawn_eg",       "8/3k1ppp/8/3K1PPP/8/8/8/8 w - - 0 1"),
    ("pos_queens_eg",     "8/5k2/8/4q3/4Q3/8/5K2/8 w - - 0 1"),
    ("pos_opp_bishops",   "8/3k2pp/5p2/8/3b4/3B4/4K2P/8 w - - 0 1"),
    ("pos_rook_vs_pawn",  "8/8/8/R7/k7/8/1p6/1K6 b - - 0 1"),
    ("pos_king_race",     "8/6pk/8/8/8/8/KP6/8 w - - 0 1"),
    ("pos_complex_pcs",   "r3r1k1/pp3pbp/2pp1np1/q3p3/2P1P3/2NP2PP/PP1Q1PB1/R3R1K1 w - - 0 14"),
    ("pos_hanging_pawns", "r2q1rk1/pp1bppbp/2np1np1/8/2pPP3/2N2N2/PP2BPPP/R1BQ1RK1 w - - 0 10"),
    ("pos_ruy_lopez",     "r1bqk2r/1bpp1ppp/p1n2n2/1p2p3/B3P3/5N2/PPPP1PPP/RNBQR1K1 b kq - 0 7"),
    ("pos_tactics_wac",   "2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - 0 1"),
    ("pos_eg_bishops",    "8/3k4/8/3p1p2/3B1B2/8/4K3/8 w - - 0 1"),
    ("pos_rook_ending",   "1r4k1/5ppp/8/pP6/8/8/5PPP/1R4K1 w - - 0 1"),
    ("pos_sym_pawns",     "6k1/pp4pp/8/8/8/8/PP4PP/6K1 w - - 0 1"),
]

# Names of the params the tuner knows about (must match UCI option names)
TUNABLE_PARAMS = {
    "LmrBase", "LmrDivisor", "LmrHistDiv", "RfpMargin", "RfpImprovingSub",
    "NmpBaseR", "NmpDepthDiv", "NmpEvalDiv", "FpBase", "FpDepthScale",
    "SeeQuietScale", "SeeCaptScale", "HistPruneScale", "AspDelta",
}


# ── Param sweep ranges for coordinate descent ──────────────────────────────────
# Defaults read from engine at runtime (single source of truth).
# These sweep ranges are centered/expanded around the current tuned values.
SWEEP_RANGES = [
    # LMR — widest impact, sweep densely around current best (0.70, 1.55, 9000)
    ("LmrBase",        [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]),
    ("LmrDivisor",     [1.35, 1.40, 1.45, 1.50, 1.55, 1.60, 1.65, 1.70, 1.80, 1.90]),
    ("LmrHistDiv",     [6000, 7000, 8000, 9000, 10000, 11000, 12000]),
    # RFP (current best: 50, 40)
    ("RfpMargin",      [25, 30, 35, 40, 45, 50, 55, 60, 65, 70]),
    ("RfpImprovingSub",[20, 30, 35, 40, 45, 50, 60]),
    # NMP (current best: 4, 4, 200)
    ("NmpBaseR",       [2, 3, 4, 5]),
    ("NmpDepthDiv",    [3, 4, 5]),
    ("NmpEvalDiv",     [125, 150, 175, 200, 225, 250]),
    # Futility (current best: 60, 65)
    ("FpBase",         [20, 35, 50, 60, 70, 80, 90, 100]),
    ("FpDepthScale",   [45, 55, 60, 65, 70, 75, 80, 90]),
    # SEE (current best: 20, 100)
    ("SeeQuietScale",  [8, 12, 16, 20, 24, 28, 32, 40]),
    ("SeeCaptScale",   [70, 80, 90, 100, 110, 120, 130, 145, 160]),
    # History pruning (current best: 2500)
    ("HistPruneScale", [1500, 2000, 2500, 3000, 3500, 4000]),
    # Aspiration (current best: 50)
    ("AspDelta",       [20, 30, 40, 45, 50, 55, 60, 70, 80]),
]


def read_engine_defaults(engine_path: Path) -> dict:
    """
    Read SearchParams defaults directly from the engine's 'uci' output.
    This makes search.h the single source of truth — no need to keep
    DEFAULT_PARAMS in sync manually.
    """
    proc = subprocess.run(
        [str(engine_path)], input="uci\nquit\n",
        capture_output=True, text=True, timeout=15,
    )
    defaults = {}
    for line in proc.stdout.splitlines():
        m = re.match(r'option name (\S+) type (spin|string) default (\S+)', line)
        if m:
            name, typ, val = m.group(1), m.group(2), m.group(3)
            if name not in TUNABLE_PARAMS:
                continue
            if typ == "spin":
                defaults[name] = int(val)
            else:
                try:
                    defaults[name] = float(val)
                except ValueError:
                    defaults[name] = val
    missing = TUNABLE_PARAMS - set(defaults.keys())
    if missing:
        raise RuntimeError(
            f"Engine did not report defaults for: {missing}\n"
            f"Check that UCI setoption strings use SP.* in uci.cpp."
        )
    return defaults


def run_single_fen(engine_path: Path, fen: str, depth: int, params: dict = None,
                   hash_mb: int = 64, timeout: int = 120) -> dict:
    """
    Run 'go depth N' on a single FEN using any UCI engine.
    Returns {nodes, nps, seldepth, bestmove}.  Works with any UCI engine (SF, etc.)
    Uses Popen + line-by-line reading so stdin stays open until bestmove is seen —
    needed for engines (e.g. Stockfish) that poll stdin while searching.
    """
    lines = ["uci"]
    lines.append(f"setoption name Hash value {hash_mb}")
    lines.append("setoption name Threads value 1")
    if params:
        for name, value in params.items():
            lines.append(f"setoption name {name} value {value}")
    lines.append("isready")
    if fen in ("startpos", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"):
        lines.append("position startpos")
    else:
        lines.append(f"position fen {fen}")
    lines.append(f"go depth {depth}")

    proc = subprocess.Popen(
        [str(engine_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )

    for cmd in lines:
        proc.stdin.write(cmd + "\n")
    proc.stdin.flush()

    result = {"nodes": 0, "nps": 0, "seldepth": 0, "bestmove": "?"}
    last_nodes = last_nps = last_sd = 0
    import time as _time
    deadline = _time.time() + timeout

    while _time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.rstrip()
        if line.startswith("info") and "nodes" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "nodes" and i + 1 < len(parts):
                    try: last_nodes = int(parts[i + 1])
                    except ValueError: pass
                elif p == "nps" and i + 1 < len(parts):
                    try: last_nps = int(parts[i + 1])
                    except ValueError: pass
                elif p == "seldepth" and i + 1 < len(parts):
                    try: last_sd = int(parts[i + 1])
                    except ValueError: pass
        elif line.startswith("bestmove"):
            parts = line.split()
            result["bestmove"] = parts[1] if len(parts) > 1 else "?"
            break

    try:
        proc.stdin.write("quit\n")
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    result["nodes"]    = last_nodes
    result["nps"]      = last_nps
    result["seldepth"] = last_sd
    return result


def compare_tree_shapes(our_engine: Path, sf_engine: Path, depth: int,
                        our_params: dict, hash_mb: int = 64) -> None:
    """
    Compare our engine's search tree vs Stockfish across all bench positions.
    Shows per-position node ratios to reveal where our pruning is weakest.
    """
    print()
    print("=" * 72)
    print(f"  TREE SHAPE vs Stockfish  (depth {depth}, 1 thread, {hash_mb}MB hash)")
    print("=" * 72)
    print(f"  {'Position':<22} {'Redux':>10} {'SF':>10} {'Ratio':>7} {'RdxSD':>6} {'SfSD':>6}")
    print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*7} {'-'*6} {'-'*6}")

    rows = []
    for name, fen in BENCH_POSITIONS:
        ours  = run_single_fen(our_engine, fen, depth, our_params, hash_mb)
        sfish = run_single_fen(sf_engine,  fen, depth, None,       hash_mb)
        ratio = ours["nodes"] / max(sfish["nodes"], 1)
        rows.append((name, ours, sfish, ratio))

    rows.sort(key=lambda r: r[3], reverse=True)  # worst ratio first
    total_ours = sum(r[1]["nodes"] for r in rows)
    total_sf   = sum(r[2]["nodes"] for r in rows)

    for name, ours, sfish, ratio in rows:
        flag = "  !!!" if ratio > 5 else ("  !!" if ratio > 3 else ("  !" if ratio > 2 else ""))
        print(f"  {name:<22} {ours['nodes']:>10,} {sfish['nodes']:>10,} {ratio:>6.1f}x"
              f" {ours['seldepth']:>6} {sfish['seldepth']:>6}{flag}")

    total_ratio = total_ours / max(total_sf, 1)
    print(f"  {'─'*22} {'─'*10} {'─'*10} {'─'*7}")
    print(f"  {'TOTAL':<22} {total_ours:>10,} {total_sf:>10,} {total_ratio:>6.1f}x")
    print(f"\n  ! = 2x  !! = 3x  !!! = 5x over SF nodes  (lower ratio = better pruning)")
    print()


def print_worst_positions(result: dict, n: int = 7) -> None:
    """Print positions sorted by node count — these are our pruning weak spots."""
    cases = sorted(result["cases"], key=lambda c: c["nodes"], reverse=True)
    total = result["total_nodes"]
    print(f"  Worst positions (most nodes = weakest pruning):")
    print(f"  {'Position':<22} {'Nodes':>10}  {'% of total':>10}")
    print(f"  {'-'*22} {'-'*10}  {'-'*10}")
    for c in cases[:n]:
        pct = c["nodes"] / total * 100 if total > 0 else 0
        print(f"  {c['name']:<22} {c['nodes']:>10,}  {pct:>9.1f}%")
    print()


def run_bench(engine_path: Path, params: dict, depth: int, threads: int,
              hash_mb: int, positions: int = 0, timeout: int = 600) -> dict:
    """
    Runs the engine bench command and returns parsed results.
    positions: if > 0, bench only first N positions (via suite file workaround not available;
               instead we filter the results to first N positions).
    """
    # Build the stdin script: setoptions then bench json
    lines = []
    for name, value in params.items():
        lines.append(f"setoption name {name} value {value}")
    lines.append(f"bench depth {depth} threads {threads} hash {hash_mb} json")
    lines.append("quit")
    stdin_text = "\n".join(lines) + "\n"

    proc = subprocess.run(
        [str(engine_path)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    stdout = proc.stdout

    # Find the JSON bench output (starts with '{' after the bench command)
    json_start = stdout.find('{\n  "config"')
    if json_start == -1:
        json_start = stdout.find('{"config"')
    if json_start == -1:
        raise RuntimeError(f"No JSON bench output found.\nstdout:\n{stdout[-2000:]}\nstderr:\n{proc.stderr[-500:]}")

    json_text = stdout[json_start:]
    # Find matching closing brace
    depth_counter = 0
    json_end = -1
    for i, ch in enumerate(json_text):
        if ch == '{':
            depth_counter += 1
        elif ch == '}':
            depth_counter -= 1
            if depth_counter == 0:
                json_end = i + 1
                break

    if json_end == -1:
        raise RuntimeError("Truncated JSON bench output")

    data = json.loads(json_text[:json_end])

    # JSON structure: { "config": {...}, "summary": { "total_nodes": N, ... }, "cases": [...] }
    summary = data.get("summary", data)  # fallback to top-level for compat
    cases = [
        {
            "name":     c.get("name", "?"),
            "depth":    c.get("depth", 0),
            "nodes":    c.get("nodes", 0),
            "bestmove": c.get("bestmove", ""),
        }
        for c in data.get("cases", [])
    ]
    if positions > 0:
        cases = cases[:positions]
    result = {
        "total_nodes": sum(c["nodes"] for c in cases),
        "total_nps":   summary.get("total_nps", 0),
        "cases": cases,
    }
    return result


def bestmoves_match(baseline: dict, candidate: dict, tolerance: int = 0) -> bool:
    """Check that bestmoves agree across all bench positions."""
    bm_base = {c["name"]: c["bestmove"] for c in baseline["cases"]}
    bm_cand = {c["name"]: c["bestmove"] for c in candidate["cases"]}
    mismatches = 0
    for name in bm_base:
        if name in bm_cand and bm_base[name] != bm_cand[name]:
            mismatches += 1
    return mismatches <= tolerance


def pct_change(baseline_nodes: int, candidate_nodes: int) -> float:
    """Return percentage node reduction (positive = fewer nodes = better)."""
    if baseline_nodes == 0:
        return 0.0
    return (baseline_nodes - candidate_nodes) / baseline_nodes * 100.0


def main():
    parser = argparse.ArgumentParser(description="Redux search parameter tuner")
    parser.add_argument("--engine",   default=str(ENGINE_DEFAULT))
    parser.add_argument("--depth",    type=int, default=8)
    parser.add_argument("--threads",  type=int, default=1)
    parser.add_argument("--hash",     type=int, default=64)
    parser.add_argument("--iters",    type=int, default=5)
    parser.add_argument("--positions", type=int, default=0,
                        help="Limit bench to first N positions (0=all)")
    parser.add_argument("--timeout",  type=int, default=900,
                        help="Per-bench timeout in seconds (default 900)")
    parser.add_argument("--output",   default=None)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--params",   default=None, help="JSON file with param set to bench")
    parser.add_argument("--sf",       default=str(SF_DEFAULT),
                        help="UCI engine for tree-shape comparison (default: Stockfish)")
    parser.add_argument("--no-sf",    action="store_true", help="Skip Stockfish comparison")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    engine_path = Path(args.engine)
    if not engine_path.exists():
        print(f"ERROR: engine not found at {engine_path}", file=sys.stderr)
        sys.exit(1)

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(args.output) if args.output else RESULTS_DIR / f"tune_{timestamp}.json"

    bench_kwargs = dict(depth=args.depth, threads=args.threads, hash_mb=args.hash,
                        positions=args.positions, timeout=args.timeout)

    # ── Read defaults from engine (single source of truth) ────────────────────
    print(f"Redux Search Tuner")
    print(f"==================")
    print(f"Engine:  {engine_path}")
    print(f"Depth:   {args.depth}  Threads: {args.threads}  Hash: {args.hash}MB")
    print(f"Reading defaults from engine...")
    DEFAULT_PARAMS = read_engine_defaults(engine_path)
    print(f"  {len(DEFAULT_PARAMS)} params: " +
          ", ".join(f"{k}={v}" for k, v in sorted(DEFAULT_PARAMS.items())))
    print()

    # ── Baseline run ────────────────────────────────────────────────────────────
    print("Running baseline bench...")
    t0 = time.time()
    baseline_result = run_bench(engine_path, DEFAULT_PARAMS, **bench_kwargs)
    baseline_time = time.time() - t0
    baseline_nodes = baseline_result["total_nodes"]
    print(f"  Baseline: {baseline_nodes:,} nodes  "
          f"{baseline_result['total_nps']:,} nps  ({baseline_time:.1f}s)")
    print()
    print_worst_positions(baseline_result)

    if args.baseline_only:
        sf_path = Path(args.sf)
        if not args.no_sf and sf_path.exists():
            compare_tree_shapes(engine_path, sf_path, args.depth, DEFAULT_PARAMS, args.hash)
        elif not args.no_sf:
            print(f"  (Stockfish not found at {sf_path})")
        return

    # ── Single-param evaluation mode ────────────────────────────────────────────
    if args.params:
        with open(args.params) as f:
            custom_params = json.load(f)
        merged = deepcopy(DEFAULT_PARAMS)
        merged.update(custom_params)
        print(f"Benchmarking custom params from {args.params}...")
        result = run_bench(engine_path, merged, **bench_kwargs)
        delta_pct = pct_change(baseline_nodes, result["total_nodes"])
        match = bestmoves_match(baseline_result, result, tolerance=1)
        print(f"  Nodes: {result['total_nodes']:,}  ({delta_pct:+.1f}% vs baseline)  "
              f"moves_match={match}")
        print(json.dumps({"params": merged, "result": result, "baseline_nodes": baseline_nodes,
                          "delta_pct": round(delta_pct, 2), "bestmoves_match": match}, indent=2))
        return

    # ── Coordinate descent ───────────────────────────────────────────────────────
    best_params  = deepcopy(DEFAULT_PARAMS)
    best_nodes   = baseline_nodes
    history      = []

    for iteration in range(1, args.iters + 1):
        print(f"-- Iteration {iteration}/{args.iters} "
              + "-" * max(0, 50 - len(str(iteration)) - len(str(args.iters))))
        improved_this_iter = False

        for param_name, candidates in SWEEP_RANGES:
            if param_name not in best_params:
                continue  # param not in this engine's tunable set
            current_value = best_params[param_name]
            param_best_nodes = best_nodes
            param_best_value = current_value

            for cand_value in candidates:
                if cand_value == current_value:
                    continue  # already know baseline

                trial_params = deepcopy(best_params)
                trial_params[param_name] = cand_value

                try:
                    result = run_bench(engine_path, trial_params, **bench_kwargs)
                except Exception as e:
                    print(f"  [{param_name}={cand_value}] ERROR: {e}")
                    continue

                nodes = result["total_nodes"]
                delta = pct_change(best_nodes, nodes)
                match = bestmoves_match(baseline_result, result, tolerance=1)

                print(f"  {param_name}={cand_value:>8}  nodes={nodes:>12,}  "
                      f"delta={delta:+6.1f}%  match={match}")

                if nodes < param_best_nodes and match:
                    param_best_nodes = nodes
                    param_best_value = cand_value

            if param_best_value != current_value:
                improvement = pct_change(best_nodes, param_best_nodes)
                print(f"  * {param_name}: {current_value} -> {param_best_value}  "
                      f"({improvement:+.1f}% nodes)")
                best_params[param_name] = param_best_value
                best_nodes = param_best_nodes
                improved_this_iter = True
                history.append({
                    "iteration": iteration,
                    "param": param_name,
                    "old_value": current_value,
                    "new_value": param_best_value,
                    "nodes": param_best_nodes,
                    "delta_pct": round(improvement, 2),
                })
            else:
                print(f"  = {param_name}: kept {current_value}")

        if not improved_this_iter:
            print(f"\nNo improvement in iteration {iteration}, converged early.")
            break

        print()

    # ── Final summary ─────────────────────────────────────────────────────────
    total_improvement = pct_change(baseline_nodes, best_nodes)
    print()
    print("=" * 56)
    print(f"TUNING COMPLETE")
    print(f"  Baseline nodes : {baseline_nodes:,}")
    print(f"  Best nodes     : {best_nodes:,}")
    print(f"  Improvement    : {total_improvement:+.1f}% node reduction")
    print(f"  (~{abs(total_improvement):.0f}% more depth capacity in same time)")
    print()
    print("Params that changed from default:")
    changed = {k: v for k, v in best_params.items() if v != DEFAULT_PARAMS.get(k)}
    if changed:
        for name, value in changed.items():
            print(f"  setoption name {name} value {value}"
                  f"  # was {DEFAULT_PARAMS.get(name)}")
    else:
        print("  (none — already at optimum)")
    print()

    # ── Final worst-position stats ─────────────────────────────────────────────
    print("Running final bench for worst-position analysis...")
    final_result = run_bench(engine_path, best_params, **bench_kwargs)
    print_worst_positions(final_result)

    # ── SF tree shape comparison ───────────────────────────────────────────────
    sf_path = Path(args.sf)
    if not args.no_sf and sf_path.exists():
        print(f"Running Stockfish comparison ({sf_path.name})...")
        compare_tree_shapes(engine_path, sf_path, args.depth, best_params, args.hash)
    elif not args.no_sf:
        print(f"  (Stockfish not found at {sf_path} — skipping comparison)")
        print(f"  Run with --sf PATH to enable, or --no-sf to suppress this message")
    print()

    # ── Save results ──────────────────────────────────────────────────────────
    output = {
        "timestamp": timestamp,
        "engine": str(engine_path),
        "depth": args.depth,
        "threads": args.threads,
        "hash_mb": args.hash,
        "baseline_nodes": baseline_nodes,
        "best_nodes": best_nodes,
        "total_improvement_pct": round(total_improvement, 2),
        "best_params": best_params,
        "default_params": DEFAULT_PARAMS,
        "changed_params": changed,
        "history": history,
    }
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Results saved to: {output_path}")


if __name__ == "__main__":
    main()
