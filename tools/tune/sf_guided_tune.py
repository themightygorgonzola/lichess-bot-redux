#!/usr/bin/env python3
"""
tools/tune/sf_guided_tune.py — SF-guided coordinate descent for Redux search params.

Objective: minimize the distance between our search tree and Stockfish's tree.
Three signals are measured per position at each candidate param set:

  • move   — mean cp cost of our chosen move vs SF's best (from SF multipv scores).
              0 = we played SF's top choice.  Continuous gradient, not binary 0/1.
  • eval   — |our_score_dN - sf_score| in centipawns
  • fart   — max(0, |our_dN - sf| - |our_d1 - sf|) in cp.  Additive degradation:
              how many cp did the search LOSE relative to static eval?
              Never blows up (old ratio formula exploded when d1≈sf).

Composite loss = W_MOVE * move_loss + W_EVAL * eval_loss + W_FART * fart_loss
                 (all terms in units of 100cp for comparability)

Positions where our deep search still diverges > FILTER_THRESH cp from SF are
automatically filtered out before tuning — they are eval-dominated and search
params cannot fix them; including them warps the tuning signal.

This replaces tune_search.py's node-count objective, which optimized for pruning
efficiency without any signal about eval quality.  A search that prunes away
refutations of bad evals scores perfectly on node count but horribly here.

Eval params are compiled-in and cannot be varied at runtime yet.  Once key eval
params are exposed as UCI options in uci.cpp / cmd_setoption they can be added to
SWEEP_RANGES and tuned in the same loop.

Usage
-----
    python tools/tune/sf_guided_tune.py
    python tools/tune/sf_guided_tune.py --depth 10 --sf-depth 15 --iters 3
    python tools/tune/sf_guided_tune.py --sf-cache results/analysis/sf_cache.json
    python tools/tune/sf_guided_tune.py --baseline-only
"""
from __future__ import annotations

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
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE      = Path(__file__).resolve().parent.parent.parent
ENGINE_DEFAULT = WORKSPACE / "bot" / "engine" / "redux-hce.exe"
SF_DEFAULT     = WORKSPACE / "engines" / "stockfish-17.1" / "stockfish" / "stockfish-windows-x86-64-avx2.exe"
SUITE_DEFAULT  = WORKSPACE / "data" / "benchmarks" / "search" / "nps_core.txt"
RESULTS_DIR    = WORKSPACE / "results"

# ── Loss weights (all terms in units of 100cp for comparability) ──────────────
W_MOVE  = 1.00   # move quality — mean cp cost of our move vs SF's best / 100
W_EVAL  = 0.25   # eval error  — mean |our_score - sf_score| / 100
W_FART  = 0.15   # fart delta  — mean max(0, search degraded vs d1) in cp / 100

SF_MULTIPV    = 5    # top-N moves to cache per position
FILTER_THRESH = 350  # cp: exclude positions where our deep search diverges > N cp
                     # raised from 200 — at 200 the filter removed positions where SEE
                     # pruning matters most, creating a blindspot that caused see_capt_scale
                     # to be tuned to 160 (vs b80's correct 90) in b92.
FILTER_DEPTH  = 14   # our engine depth for eval-dominated filter

# ── ANSI ──────────────────────────────────────────────────────────────────────
try:
    import ctypes as _c
    _c.windll.kernel32.SetConsoleMode(_c.windll.kernel32.GetStdHandle(-11), 7)
except Exception:
    pass

def _esc(code: str, t: str) -> str:  return f"\033[{code}m{t}\033[0m"
def green(t):  return _esc("92", t)
def red(t):    return _esc("91", t)
def yellow(t): return _esc("93", t)
def bold(t):   return _esc("1",  t)
def dim(t):    return _esc("2",  t)

# ── Tunable search params (must match UCI option names in uci.cpp) ─────────────
TUNABLE_PARAMS = {
    "LmrBase", "LmrDivisor", "LmrHistDiv", "RfpMargin", "RfpImprovingSub",
    "NmpBaseR", "NmpDepthDiv", "NmpEvalDiv", "FpBase", "FpDepthScale",
    "SeeQuietScale", "SeeCaptScale", "HistPruneScale", "AspDelta",
}

# Sweep ranges for coordinate descent.  Keeps node-count-tuned b89 values as
# candidates alongside the pre-tune b80 values so we don't regress good changes.
SWEEP_RANGES = [
    ("LmrBase",        [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]),
    ("LmrDivisor",     [1.35, 1.40, 1.45, 1.50, 1.55, 1.60, 1.65, 1.70, 1.80, 1.90]),
    ("LmrHistDiv",     [6000, 7000, 8000, 9000, 10000, 11000, 12000]),
    ("RfpMargin",      [25, 30, 35, 40, 45, 50, 55, 60, 65, 70]),
    ("RfpImprovingSub",[20, 30, 35, 40, 45, 50, 60]),
    ("NmpBaseR",       [2, 3, 4, 5]),
    ("NmpDepthDiv",    [3, 4, 5]),
    ("NmpEvalDiv",     [125, 150, 175, 200, 225, 250, 300]),
    ("FpBase",         [20, 35, 50, 60, 70, 80, 90, 100]),
    ("FpDepthScale",   [45, 55, 60, 65, 70, 75, 80, 90]),
    ("SeeQuietScale",  [8, 12, 16, 20, 24, 28, 32, 40]),
    ("SeeCaptScale",   [70, 80, 90, 100, 110, 120, 130, 145, 160]),
    ("HistPruneScale", [1500, 2000, 2500, 3000, 3500, 4000]),
    ("AspDelta",       [20, 30, 40, 45, 50, 55, 60, 70, 80]),
]


# ── Position suite ─────────────────────────────────────────────────────────────
def load_suite(path: Path) -> list[tuple[str, str]]:
    """Load name|fen pairs from file, skip blank lines and comments."""
    positions = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, fen = line.split("|", 1)
                positions.append((name.strip(), fen.strip()))
    return positions


# ── UCI engine query ───────────────────────────────────────────────────────────
def run_fen_detailed(engine_path: Path, fen: str, depth: int,
                     params: Optional[dict] = None,
                     hash_mb: int = 64, timeout: int = 120) -> dict:
    """
    Run 'go depth N' on a single FEN.  Returns:
      {bestmove, score_cp, nodes,
       d1_score_cp, d1_bestmove,       ← for fart calculation
       per_depth: {d: {score_cp, bestmove}}}

    Captures all info-depth lines in a single search so d1 is free.
    Works with any UCI engine (Redux, Stockfish).
    """
    cmds = ["uci",
            f"setoption name Hash value {hash_mb}",
            "setoption name Threads value 1"]
    if params:
        for name, val in params.items():
            cmds.append(f"setoption name {name} value {val}")
    cmds.append("isready")
    if fen in ("startpos", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"):
        cmds.append("position startpos")
    else:
        cmds.append(f"position fen {fen}")
    cmds.append(f"go depth {depth}")

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [str(engine_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, creationflags=flags,
    )
    for cmd in cmds:
        proc.stdin.write(cmd + "\n")
    proc.stdin.flush()

    per_depth: dict[int, dict] = {}
    last_nodes = 0
    bestmove = "?"
    deadline = time.time() + timeout

    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.rstrip()

        if line.startswith("info") and "depth" in line and "score" in line:
            # Skip "info string" lines
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "string":
                continue
            d = _parse_token_int(parts, "depth")
            if d is None:
                continue
            # score cp X  or  score mate M
            sc = _parse_score(parts)
            pv_move = _parse_pv_move(parts)
            nodes = _parse_token_int(parts, "nodes") or last_nodes
            last_nodes = nodes
            if d not in per_depth or sc is not None:
                per_depth[d] = {"score_cp": sc, "bestmove": pv_move}

        elif line.startswith("bestmove"):
            p = line.split()
            bestmove = p[1] if len(p) > 1 else "?"
            break

    try:
        proc.stdin.write("quit\n"); proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    final = per_depth.get(depth) or (per_depth[max(per_depth)] if per_depth else {})
    d1    = per_depth.get(1, {})
    return {
        "bestmove":    bestmove,
        "score_cp":    final.get("score_cp"),
        "nodes":       last_nodes,
        "d1_score_cp": d1.get("score_cp"),
        "d1_bestmove": d1.get("bestmove"),
        "per_depth":   {str(k): v for k, v in per_depth.items()},
    }


def _parse_token_int(parts: list[str], token: str) -> Optional[int]:
    for i, p in enumerate(parts):
        if p == token and i + 1 < len(parts):
            try:
                return int(parts[i + 1])
            except ValueError:
                pass
    return None


def _parse_score(parts: list[str]) -> Optional[int]:
    for i, p in enumerate(parts):
        if p == "score" and i + 2 < len(parts):
            if parts[i + 1] == "cp":
                try:
                    return max(-10000, min(10000, int(parts[i + 2])))
                except ValueError:
                    pass
            elif parts[i + 1] == "mate":
                try:
                    m = int(parts[i + 2])
                    return 10000 if m > 0 else -10000
                except ValueError:
                    pass
    return None


def _parse_pv_move(parts: list[str]) -> Optional[str]:
    for i, p in enumerate(parts):
        if p == "pv" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def run_fen_multipv(engine_path: Path, fen: str, depth: int,
                   n_pv: int = SF_MULTIPV,
                   hash_mb: int = 128, timeout: int = 300) -> dict:
    """
    Run 'go depth D' with MultiPV=N.  Returns:
      {bestmove, score_cp, d1_score_cp,
       multipv: [{move, score_cp}, ...]  ← sorted best-first by SF}

    Used to build the SF ground-truth cache.  The multipv list enables a
    continuous move-quality gradient (vs binary agree/disagree).
    """
    cmds = [
        "uci",
        f"setoption name Hash value {hash_mb}",
        "setoption name Threads value 1",
        f"setoption name MultiPV value {n_pv}",
        "isready",
    ]
    if fen in ("startpos", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"):
        cmds.append("position startpos")
    else:
        cmds.append(f"position fen {fen}")
    cmds.append(f"go depth {depth}")

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [str(engine_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, creationflags=flags,
    )
    for cmd in cmds:
        proc.stdin.write(cmd + "\n")
    proc.stdin.flush()

    # latest[k] = {move, score_cp} — updated each time we see multipv k;
    # final values are from the last (deepest) completed iteration.
    latest: dict[int, dict] = {}
    d1_score: Optional[int] = None
    bestmove = "?"
    deadline = time.time() + timeout

    import threading, queue as _q
    out_q: _q.Queue = _q.Queue()
    def _reader(pipe, q):
        for ln in pipe:
            q.put(ln)
        q.put(None)  # sentinel
    _t = threading.Thread(target=_reader, args=(proc.stdout, out_q), daemon=True)
    _t.start()

    while time.time() < deadline:
        try:
            line = out_q.get(timeout=0.1)
        except _q.Empty:
            continue
        if line is None:
            break
        line = line.rstrip()

        if line.startswith("info") and "multipv" in line:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "string":
                continue
            d  = _parse_token_int(parts, "depth")
            k  = _parse_token_int(parts, "multipv")
            sc = _parse_score(parts)
            mv = _parse_pv_move(parts)
            if d is not None and k is not None and sc is not None and mv:
                latest[k] = {"move": mv, "score_cp": sc}
                if d == 1 and k == 1:
                    d1_score = sc

        elif line.startswith("bestmove"):
            p = line.split()
            bestmove = p[1] if len(p) > 1 else "?"
            break

    try:
        proc.stdin.write("quit\n"); proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    multipv_list = [latest[k] for k in sorted(latest)]
    best_score   = multipv_list[0]["score_cp"] if multipv_list else None
    return {
        "bestmove":    bestmove,
        "score_cp":    best_score,
        "d1_score_cp": d1_score,
        "multipv":     multipv_list,
    }


# ── SF cache ───────────────────────────────────────────────────────────────────
def build_sf_cache(sf_path: Path, positions: list[tuple[str, str]],
                   sf_depth: int, hash_mb: int = 128) -> dict:
    """
    Run Stockfish with MultiPV on all positions.
    Returns {name: {bestmove, score_cp, d1_score_cp, multipv: [{move, score_cp}...]}}.
    SF is slow; cache and reuse across tuner runs.
    """
    print(f"\n  Running SF d{sf_depth} multipv{SF_MULTIPV} on {len(positions)} positions (one-time)...")
    cache = {}
    for i, (name, fen) in enumerate(positions, 1):
        print(f"  [{i:2d}/{len(positions)}] {name} ...", end=" ", flush=True)
        r = run_fen_multipv(sf_path, fen, sf_depth, n_pv=SF_MULTIPV,
                            hash_mb=hash_mb, timeout=300)
        cache[name] = {
            "fen":         fen,
            "bestmove":    r["bestmove"],
            "score_cp":    r["score_cp"],
            "d1_score_cp": r["d1_score_cp"],
            "multipv":     r["multipv"],
        }
        top3 = " ".join(e["move"] for e in r["multipv"][:3])
        print(f"score={r['score_cp']}  top3=[{top3}]")
    return cache


def load_or_build_sf_cache(cache_path: Optional[Path],
                            sf_path: Path,
                            positions: list[tuple[str, str]],
                            sf_depth: int,
                            hash_mb: int = 128,
                            force_rebuild: bool = False) -> dict:
    if cache_path and cache_path.exists() and not force_rebuild:
        with open(cache_path) as f:
            data = json.load(f)
        cached = data.get("results", data)  # handle both wrapped and raw
        # Check all positions are covered
        missing    = [n for n, _ in positions if n not in cached]
        no_multipv = [n for n in cached if not cached[n].get("multipv")]
        if not missing and not no_multipv:
            print(f"  Loaded SF cache from {cache_path}  ({len(cached)} positions)")
            return cached
        if missing:
            print(f"  SF cache missing {len(missing)} positions: {missing[:5]}...")
        if no_multipv:
            print(f"  SF cache lacks multipv data ({len(no_multipv)} entries), rebuilding...")

    cache = build_sf_cache(sf_path, positions, sf_depth, hash_mb)

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump({"sf_depth": sf_depth, "results": cache}, f, indent=2)
        print(f"  SF cache saved to {cache_path}")

    return cache


# ── Loss function ──────────────────────────────────────────────────────────────
def compute_loss(our_results: list[dict], sf_cache: dict,
                 positions: list[tuple[str, str]]) -> dict:
    """
    Composite loss — all terms in units of 100cp.

    move_loss:  mean cp cost of our chosen move vs SF's best, divided by 100.
                Uses SF's MultiPV scores → continuous gradient, not binary 0/1.
                Our move = SF top-1 → 0.  Our move 200cp worse → 2.0.
                Not in top-N → worst_multipv_score − 30cp penalty.

    eval_loss:  mean |our_dN_score − sf_score| / 100.

    fart_loss:  mean max(0, |our_dN − sf| − |our_d1 − sf|) / 100.
                Additive: only penalises when search made things worse vs static
                eval.  Never blows up (old ratio exploded when d1 ≈ sf).

    total = W_MOVE * move_loss + W_EVAL * eval_loss + W_FART * fart_loss
    """
    move_costs  = []
    eval_errs   = []
    fart_deltas = []

    for i, (name, _fen) in enumerate(positions):
        sf   = sf_cache.get(name, {})
        ours = our_results[i] if i < len(our_results) else {}

        sf_score   = sf.get("score_cp")
        sf_multipv = sf.get("multipv", [])
        our_bm     = ours.get("bestmove")
        our_score  = ours.get("score_cp")
        our_d1     = ours.get("d1_score_cp")

        # ── Move quality (continuous, via SF multipv) ──────────────────────────
        if sf_score is not None and our_bm and our_bm not in ("?", None):
            our_move_sf_score: Optional[int] = None
            for entry in sf_multipv:
                if entry.get("move") == our_bm:
                    our_move_sf_score = entry["score_cp"]
                    break
            if our_move_sf_score is None and sf_multipv:
                # Not in top-N: penalise as worst multipv score minus a gap
                worst = min(e["score_cp"] for e in sf_multipv)
                our_move_sf_score = worst - 30
            if our_move_sf_score is not None:
                move_costs.append(max(0, sf_score - our_move_sf_score))

        # ── Eval error ─────────────────────────────────────────────────────────
        if sf_score is not None and our_score is not None:
            eval_errs.append(abs(our_score - sf_score))

        # ── Fart delta (additive, clipped — only penalise degradation) ─────────
        if our_d1 is not None and our_score is not None and sf_score is not None:
            d1_err = abs(our_d1   - sf_score)
            dn_err = abs(our_score - sf_score)
            fart_deltas.append(max(0, dn_err - d1_err))

    move_loss = (sum(move_costs)  / len(move_costs)  / 100.0) if move_costs  else 0.0
    eval_loss = (sum(eval_errs)   / len(eval_errs)   / 100.0) if eval_errs   else 0.0
    fart_loss = (sum(fart_deltas) / len(fart_deltas) / 100.0) if fart_deltas else 0.0
    total     = W_MOVE * move_loss + W_EVAL * eval_loss + W_FART * fart_loss

    return {
        "total":              total,
        "move_loss":          move_loss,
        "eval_loss":          eval_loss,
        "fart_loss":          fart_loss,
        "n_positions":        len(positions),
        "mean_move_cost_cp":  round(sum(move_costs)  / len(move_costs))  if move_costs  else None,
        "mean_eval_err_cp":   round(sum(eval_errs)   / len(eval_errs))   if eval_errs   else None,
        "mean_fart_delta_cp": round(sum(fart_deltas) / len(fart_deltas)) if fart_deltas else None,
    }


def evaluate_params(engine_path: Path, positions: list[tuple[str, str]],
                    params: dict, depth: int, hash_mb: int) -> list[dict]:
    """Run our engine on all positions with given params."""
    results = []
    for _name, fen in positions:
        r = run_fen_detailed(engine_path, fen, depth, params, hash_mb, timeout=180)
        results.append(r)
    return results


def format_loss(loss: dict) -> str:
    total = loss["total"]
    colour = green if total < 0.4 else (yellow if total < 0.8 else red)
    return (colour(f"{total:.4f}") +
            f"  [move={loss['move_loss']:.3f}"
            f" eval={loss['eval_loss']:.3f}"
            f" fart={loss['fart_loss']:.3f}]"
            f"  (mv_cost={loss.get('mean_move_cost_cp', '—')}cp"
            f"  eval_err={loss.get('mean_eval_err_cp', '—')}cp"
            f"  fart_Δ={loss.get('mean_fart_delta_cp', '—')}cp)")


# ── Suite filter: remove eval-dominated positions ─────────────────────────────
def filter_positions(engine_path: Path, positions: list[tuple[str, str]],
                     sf_cache: dict, filter_depth: int,
                     filter_thresh: int) -> list[tuple[str, str]]:
    """
    Run our engine at filter_depth and discard positions where
    |our_score - sf_score| > filter_thresh cp.  These are eval-dominated:
    search params cannot fix large structural eval errors, and including them
    warps the loss landscape so the tuner adjusts pruning to hide the symptoms.
    """
    print(f"\n  Filtering eval-dominated positions "
          f"(d{filter_depth}, thresh={filter_thresh}cp)...")
    kept, removed = [], []
    for name, fen in positions:
        sf_score = sf_cache.get(name, {}).get("score_cp")
        if sf_score is None:          # can't filter without SF reference
            kept.append((name, fen))
            continue
        r = run_fen_detailed(engine_path, fen, filter_depth,
                             params=None, hash_mb=64, timeout=120)
        our_score = r.get("score_cp")
        if our_score is None or abs(our_score - sf_score) <= filter_thresh:
            kept.append((name, fen))
        else:
            removed.append((name, abs(our_score - sf_score)))
    if removed:
        print(f"  Removed {len(removed)} eval-dominated positions "
              f"(>{filter_thresh}cp at d{filter_depth}):")
        for nm, err in sorted(removed, key=lambda x: -x[1]):
            print(f"    {nm:<26} {err}cp error")
    else:
        print(f"  All {len(positions)} positions pass filter "
              f"(≤{filter_thresh}cp at d{filter_depth})")
    print(f"  Tuning on {len(kept)}/{len(positions)} positions")
    return kept


# ── Read current params from engine ───────────────────────────────────────────
def read_engine_defaults(engine_path: Path) -> dict:
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
            defaults[name] = int(val) if typ == "spin" else (
                float(val) if re.match(r'^-?\d+\.?\d*$', val) else val)
    missing = TUNABLE_PARAMS - set(defaults.keys())
    if missing:
        raise RuntimeError(f"Engine did not report UCI defaults for: {missing}")
    return defaults


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine",       default=str(ENGINE_DEFAULT))
    ap.add_argument("--sf",           default=str(SF_DEFAULT))
    ap.add_argument("--suite",        default=str(SUITE_DEFAULT),
                    help="Position suite (name|fen format, default: nps_core.txt)")
    ap.add_argument("--depth",        type=int, default=10,
                    help="Our engine search depth per position (default: 10)")
    ap.add_argument("--sf-depth",     type=int, default=15,
                    help="SF search depth for ground truth (default: 15)")
    ap.add_argument("--sf-cache",     default=str(RESULTS_DIR / "analysis" / "sf_guide_cache.json"),
                    help="Path to SF results cache JSON (built on first run)")
    ap.add_argument("--rebuild-sf",   action="store_true",
                    help="Force rebuild SF cache even if it exists")
    ap.add_argument("--iters",        type=int, default=5)
    ap.add_argument("--hash",         type=int, default=64)
    ap.add_argument("--baseline-only",action="store_true")
    ap.add_argument("--output",       default=None,
                    help="Save results JSON to this path")
    ap.add_argument("--limit",        type=int, default=0,
                    help="Limit positions to first N (0=all)")
    ap.add_argument("--filter-thresh", type=int, default=FILTER_THRESH,
                    help=f"cp threshold for eval-dominated filter (default: {FILTER_THRESH}, 0=off)")
    ap.add_argument("--filter-depth",  type=int, default=FILTER_DEPTH,
                    help=f"Our engine depth used for pre-filter (default: {FILTER_DEPTH})")
    ap.add_argument("--no-filter",     action="store_true",
                    help="Skip eval-dominated position filtering")
    args = ap.parse_args()

    engine_path = Path(args.engine)
    sf_path     = Path(args.sf)
    suite_path  = Path(args.suite)
    cache_path  = Path(args.sf_cache)

    if not engine_path.exists():
        print(f"ERROR: engine not found: {engine_path}", file=sys.stderr); sys.exit(1)
    if not sf_path.exists():
        print(f"ERROR: SF not found: {sf_path}", file=sys.stderr); sys.exit(1)
    if not suite_path.exists():
        print(f"ERROR: suite not found: {suite_path}", file=sys.stderr); sys.exit(1)

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(args.output) if args.output else RESULTS_DIR / f"sf_guided_{timestamp}.json"

    positions = load_suite(suite_path)
    if args.limit:
        positions = positions[:args.limit]

    print(bold("\n  SF-Guided Search Tuner"))
    print(f"  {'─'*60}")
    print(f"  Engine  : {engine_path.name}")
    print(f"  SF      : {sf_path.name}")
    print(f"  Suite   : {suite_path.name}  ({len(positions)} positions)")
    print(f"  Depths  : ours=d{args.depth}  SF=d{args.sf_depth}")
    print(f"  Loss    : {W_MOVE}*move_quality + {W_EVAL}*eval_err + {W_FART}*fart_delta  (units: 100cp)")
    print(f"  Iters   : {args.iters}")

    # ── Read engine defaults ──────────────────────────────────────────────────
    print(f"\n  Reading engine defaults...")
    DEFAULT_PARAMS = read_engine_defaults(engine_path)
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(DEFAULT_PARAMS.items())))

    # ── Build / load SF cache ─────────────────────────────────────────────────
    sf_cache = load_or_build_sf_cache(
        cache_path, sf_path, positions, args.sf_depth,
        hash_mb=128, force_rebuild=args.rebuild_sf)

    # ── Filter eval-dominated positions ───────────────────────────────────────
    if not args.no_filter and args.filter_thresh > 0:
        positions = filter_positions(engine_path, positions, sf_cache,
                                     args.filter_depth, args.filter_thresh)
        if not positions:
            print("  ERROR: all positions filtered out — lower --filter-thresh or use --no-filter")
            sys.exit(1)

    # ── Baseline ──────────────────────────────────────────────────────────────
    print(f"\n  Computing baseline loss (d{args.depth}, current params)...")
    t0 = time.time()
    baseline_results = evaluate_params(engine_path, positions, DEFAULT_PARAMS,
                                       args.depth, args.hash)
    baseline_loss = compute_loss(baseline_results, sf_cache, positions)
    print(f"  Baseline: {format_loss(baseline_loss)}  ({time.time()-t0:.1f}s)")

    if args.baseline_only:
        print("\n  Per-position detail:")
        _print_position_detail(positions, baseline_results, sf_cache)
        return

    # ── Coordinate descent ────────────────────────────────────────────────────
    best_params = deepcopy(DEFAULT_PARAMS)
    best_loss   = baseline_loss["total"]
    history     = []

    for iteration in range(1, args.iters + 1):
        print(f"\n{'─'*72}")
        print(f"  Iteration {iteration}/{args.iters}")
        print(f"{'─'*72}")
        improved_this_iter = False

        for param_name, candidates in SWEEP_RANGES:
            if param_name not in best_params:
                continue
            current_value    = best_params[param_name]
            param_best_loss  = best_loss
            param_best_value = current_value

            for cand_value in candidates:
                if cand_value == current_value:
                    continue

                trial_params = deepcopy(best_params)
                trial_params[param_name] = cand_value

                try:
                    trial_results = evaluate_params(engine_path, positions,
                                                    trial_params, args.depth, args.hash)
                except Exception as e:
                    print(f"  [{param_name}={cand_value}] ERROR: {e}")
                    continue

                loss = compute_loss(trial_results, sf_cache, positions)
                delta = param_best_loss - loss["total"]
                better = loss["total"] < param_best_loss

                indicator = green("↓") if better else dim("·")
                print(f"  {indicator} {param_name}={cand_value:>8}  "
                      f"loss={loss['total']:.4f}  "
                      f"mv={loss.get('mean_move_cost_cp','—')}cp  "
                      f"eval={loss.get('mean_eval_err_cp','—')}cp  "
                      f"fartΔ={loss.get('mean_fart_delta_cp','—')}cp"
                      + (green(f"  Δ{delta:+.4f}") if better else ""))

                if better:
                    param_best_loss  = loss["total"]
                    param_best_value = cand_value

            if param_best_value != current_value:
                improvement = best_loss - param_best_loss
                print(f"  ★ {param_name}: {current_value} → {bold(str(param_best_value))}"
                      f"  (loss {best_loss:.4f} → {param_best_loss:.4f}"
                      f"  Δ{improvement:+.4f})")
                best_params[param_name] = param_best_value
                best_loss = param_best_loss
                improved_this_iter = True
                history.append({
                    "iteration":  iteration,
                    "param":      param_name,
                    "old_value":  current_value,
                    "new_value":  param_best_value,
                    "loss_after": round(param_best_loss, 5),
                    "delta":      round(improvement, 5),
                })
            else:
                print(f"  = {param_name}: kept {current_value}")

        if not improved_this_iter:
            print(f"\n  No improvement in iteration {iteration}, converged.")
            break

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(bold("  SF-GUIDED TUNING COMPLETE"))
    print(f"{'='*72}")
    print(f"  Baseline loss : {baseline_loss['total']:.4f}"
          f"  (mv_cost={baseline_loss.get('mean_move_cost_cp','—')}cp"
          f"  eval_err={baseline_loss.get('mean_eval_err_cp','—')}cp"
          f"  fart_Δ={baseline_loss.get('mean_fart_delta_cp','—')}cp)")
    print(f"  Final loss    : {best_loss:.4f}")
    total_improvement = baseline_loss["total"] - best_loss
    print(f"  Improvement   : {green(f'{total_improvement:+.4f}') if total_improvement > 0 else red(f'{total_improvement:+.4f}')}")

    # Final evaluation with best params
    print(f"\n  Final evaluation with best params (d{args.depth})...")
    final_results = evaluate_params(engine_path, positions, best_params,
                                    args.depth, args.hash)
    final_loss = compute_loss(final_results, sf_cache, positions)
    print(f"  Final: {format_loss(final_loss)}")

    print(f"\n  Params changed from default:")
    changed = {k: v for k, v in best_params.items() if v != DEFAULT_PARAMS.get(k)}
    if changed:
        for name, value in sorted(changed.items()):
            print(f"    setoption name {name} value {value}"
                  f"  # was {DEFAULT_PARAMS.get(name)}")
    else:
        print("    (none — already at optimum)")

    # Per-position detail with final params
    print(f"\n  Per-position detail (final params vs SF):")
    _print_position_detail(positions, final_results, sf_cache)

    # ── Save results ──────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "timestamp":      timestamp,
            "engine":         str(engine_path),
            "depth":          args.depth,
            "sf_depth":       args.sf_depth,
            "baseline_loss":  baseline_loss,
            "final_loss":     final_loss,
            "best_params":    best_params,
            "default_params": DEFAULT_PARAMS,
            "changed_params": changed,
            "history":        history,
        }, f, indent=2)
    print(f"\n  Results saved to: {output_path}")
    print(f"{'='*72}")


def _print_position_detail(positions: list[tuple[str, str]],
                            our_results: list[dict],
                            sf_cache: dict) -> None:
    W = 72
    print(f"  {'Position':<24} {'SF_bm':<8} {'Our_bm':<8} {'SF_cp':>6} {'Our_cp':>7} {'FartΔcp':>8}  MvCost")
    print(f"  {'─'*24} {'─'*8} {'─'*8} {'─'*6} {'─'*7} {'─'*8}  {'─'*6}")
    for i, (name, _fen) in enumerate(positions):
        sf  = sf_cache.get(name, {})
        our = our_results[i] if i < len(our_results) else {}
        sf_bm      = sf.get("bestmove", "?")
        our_bm     = our.get("bestmove", "?")
        sf_sc      = sf.get("score_cp")
        our_sc     = our.get("score_cp")
        our_d1     = our.get("d1_score_cp")
        sf_multipv = sf.get("multipv", [])

        # Move quality: look up our move in SF's multipv
        our_move_sf_score: Optional[int] = None
        for entry in sf_multipv:
            if entry.get("move") == our_bm:
                our_move_sf_score = entry["score_cp"]
                break
        if sf_sc is not None and our_move_sf_score is not None:
            mv_cost: Optional[int] = max(0, sf_sc - our_move_sf_score)
        elif sf_sc is not None and our_bm not in (None, "?") and sf_multipv:
            worst   = min(e["score_cp"] for e in sf_multipv)
            mv_cost = max(0, sf_sc - worst) + 30
        else:
            mv_cost = None

        # Fart delta (additive cp, only degradation)
        fart_delta: Optional[int] = None
        if our_d1 is not None and our_sc is not None and sf_sc is not None:
            fart_delta = max(0, abs(our_sc - sf_sc) - abs(our_d1 - sf_sc))

        fart_s = (green(f"+{fart_delta:3d}") if fart_delta == 0 else
                  yellow(f"+{fart_delta:3d}") if fart_delta is not None and fart_delta < 50 else
                  red(f"+{fart_delta:3d}")   if fart_delta is not None else dim("     —  "))
        mv_s   = (green("top-1")              if mv_cost == 0 else
                  yellow(f"~{mv_cost}cp")     if mv_cost is not None and mv_cost < 50 else
                  red(f"-{mv_cost}cp")        if mv_cost is not None else red("miss "))
        sf_bm_s  = (sf_bm  or "?")[:7]
        our_bm_s = (our_bm or "?")[:7]
        sf_sc_s  = f"{sf_sc:+5d}"  if sf_sc  is not None else "   — "
        our_sc_s = f"{our_sc:+5d}" if our_sc is not None else "    — "
        print(f"  {name:<24} {sf_bm_s:<8} {our_bm_s:<8} {sf_sc_s:>6} {our_sc_s:>7} "
              f"{fart_s:>8}  {mv_s}")


if __name__ == "__main__":
    main()
