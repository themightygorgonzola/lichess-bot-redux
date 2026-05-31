#!/usr/bin/env python3
"""
tree_compare.py — Compare search tree shape: build-80 vs build-87 vs Stockfish.

For each position in a suite, runs all three engines to a fixed depth and
measures how closely each build's search tree mirrors Stockfish ("God's tree"):
  • SF-agreement rate per depth (does our bestmove match SF at each depth?)
  • First depth where each build diverges from SF
  • Node count ratio at final depth vs SF
  • Eval error vs SF at final depth (centipawns)
  • "Own-fart score": fraction of static-eval error that remains after search
    (1.0 = search added nothing, converged to d1; 0.0 = search reached SF's eval)

Usage
-----
    python tools/analysis/tree_compare.py
    python tools/analysis/tree_compare.py --depth 16
    python tools/analysis/tree_compare.py \\
        --build87 build/redux-hce.exe \\
        --build80 archives/build-80/redux-hce.exe \\
        --sf engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe \\
        --positions data/benchmarks/search/nps_core.txt \\
        --json results/analysis/tree_compare.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    # Suppress Windows Application Error dialogs from child engine processes
    import ctypes as _wctypes
    _wctypes.windll.kernel32.SetErrorMode(0x8003)  # SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX

_ROOT = Path(__file__).resolve().parent.parent.parent

# ── ANSI ─────────────────────────────────────────────────────────────────────
try:
    import ctypes as _c
    _c.windll.kernel32.SetConsoleMode(_c.windll.kernel32.GetStdHandle(-11), 7)
except Exception:
    pass

def _esc(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m"

def green(t):  return _esc("92", t)
def red(t):    return _esc("91", t)
def yellow(t): return _esc("93", t)
def bold(t):   return _esc("1",  t)
def dim(t):    return _esc("2",  t)
def cyan(t):   return _esc("96", t)

# ── Default paths ─────────────────────────────────────────────────────────────
_SF_CANDIDATES = [
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe",
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64.exe",
]
_B87_CANDIDATES = ["build/redux-hce.exe", "bot/engine/redux-hce.exe"]
_B80_CANDIDATES = ["archives/build-80/redux-hce.exe"]
_SUITE_DEFAULT  = "data/benchmarks/search/nps_core.txt"


def _find(candidates: list[str], label: str) -> str:
    for rel in candidates:
        p = _ROOT / rel
        if p.exists():
            return str(p)
    raise FileNotFoundError(f"{label} not found among: {candidates}")


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class DepthRecord:
    depth:      int
    bestmove:   Optional[str]
    score_cp:   Optional[int]
    score_mate: Optional[int]
    nodes:      int


@dataclass
class EngineResult:
    label:   str
    records: list[DepthRecord] = field(default_factory=list)

    def at_depth(self, d: int) -> Optional[DepthRecord]:
        for r in reversed(self.records):
            if r.depth == d:
                return r
        return None

    def final(self) -> Optional[DepthRecord]:
        return self.records[-1] if self.records else None


# ── Engine runner ─────────────────────────────────────────────────────────────
def _run_to_depth(engine_path: str, fen: str, max_depth: int,
                  extra_options: dict | None = None) -> list[DepthRecord]:
    """
    Run engine to max_depth on fen. Return list of DepthRecord per depth.
    """
    env = os.environ.copy()
    for p in [r"C:\mingw64\bin"]:
        if os.path.isdir(p) and p not in env.get("PATH", ""):
            env["PATH"] = p + os.pathsep + env.get("PATH", "")

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            [engine_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env, creationflags=flags,
        )
    except Exception as e:
        print(f"  [ERR] Could not start {engine_path}: {e}", file=sys.stderr)
        return []

    collected: list[str] = []
    done = threading.Event()

    def _reader():
        for line in proc.stdout:
            line = line.rstrip()
            collected.append(line)
            if line.startswith("bestmove"):
                done.set()
        done.set()  # EOF: process ended (crash or normal exit)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    def _send(msg: str):
        try:
            proc.stdin.write(msg + "\n")
            proc.stdin.flush()
        except OSError:
            pass

    _send("uci")
    deadline = time.time() + 8
    while time.time() < deadline:
        if any("uciok" in l for l in collected):
            break
        time.sleep(0.02)

    _send("setoption name Hash value 32")
    _send("setoption name Threads value 1")
    if extra_options:
        for name, val in extra_options.items():
            _send(f"setoption name {name} value {val}")

    _send("isready")
    deadline = time.time() + 8
    while time.time() < deadline:
        if any("readyok" in l for l in collected):
            break
        time.sleep(0.02)

    _send("ucinewgame")
    _send(f"position fen {fen}")
    _send(f"go depth {max_depth}")

    done.wait(timeout=90)
    _send("quit")
    t.join(timeout=3)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    finally:
        try: proc.stdin.close()
        except Exception: pass
        try: proc.stdout.close()
        except Exception: pass

    # Parse info lines
    records: list[DepthRecord] = []
    seen_depths: set[int] = set()

    for line in collected:
        parts = line.split()
        if not parts or parts[0] != "info":
            continue
        if len(parts) >= 2 and parts[1] == "string":
            continue

        depth = None
        score_cp: Optional[int] = None
        score_mate: Optional[int] = None
        nodes = 0
        pv_move: Optional[str] = None

        i = 1
        while i < len(parts):
            tok = parts[i]
            if tok == "depth" and i + 1 < len(parts):
                try: depth = int(parts[i + 1])
                except ValueError: pass
                i += 2
            elif tok == "score" and i + 2 < len(parts):
                if parts[i + 1] == "cp":
                    try: score_cp = int(parts[i + 2])
                    except ValueError: pass
                    score_mate = None
                elif parts[i + 1] == "mate":
                    try: score_mate = int(parts[i + 2])
                    except ValueError: pass
                    score_cp = None
                i += 3
            elif tok == "nodes" and i + 1 < len(parts):
                try: nodes = int(parts[i + 1])
                except ValueError: pass
                i += 2
            elif tok == "pv" and i + 1 < len(parts):
                pv_move = parts[i + 1]
                break
            else:
                i += 1

        if depth is not None and depth not in seen_depths and pv_move is not None:
            seen_depths.add(depth)
            records.append(DepthRecord(
                depth=depth, bestmove=pv_move,
                score_cp=score_cp, score_mate=score_mate, nodes=nodes,
            ))

    return sorted(records, key=lambda r: r.depth)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _cp(r: Optional[DepthRecord]) -> Optional[int]:
    """Normalize score to centipawns (±10000 for mates)."""
    if r is None:
        return None
    if r.score_mate is not None:
        return 10000 if r.score_mate > 0 else -10000
    return r.score_cp


def _fart_score(d1_cp: Optional[int], final_cp: Optional[int],
                sf_cp: Optional[int]) -> Optional[float]:
    """
    Fraction of initial static-eval error that remains after search.
    0.0 = search converged fully to SF's eval.
    1.0 = search stayed at static eval (pure 'own farts').
    >1.0 = search made things worse.
    """
    if d1_cp is None or final_cp is None or sf_cp is None:
        return None
    d1_err = abs(d1_cp - sf_cp)
    if d1_err < 5:    # already close; skip to avoid noise
        return None
    return abs(final_cp - sf_cp) / d1_err


# ── Per-position analysis ─────────────────────────────────────────────────────
def analyse_position(name: str, fen: str, max_depth: int,
                     sf_path: str, b80_path: str, b87_path: str) -> dict:
    """Run all three engines in parallel and return comparison dict."""
    results: dict[str, EngineResult] = {}

    # Run sequentially to avoid multiple heavy SF instances in parallel (crashes)
    for label, path, opts in [
        ("sf",  sf_path,  None),
        ("b80", b80_path, None),
        ("b87", b87_path, {"UseNNUE": "false"}),
    ]:
        records = _run_to_depth(path, fen, max_depth, opts)
        results[label] = EngineResult(label=label, records=records)

    sf  = results.get("sf")
    b80 = results.get("b80")
    b87 = results.get("b87")

    if not sf or not sf.records:
        return {"name": name, "fen": fen, "error": "SF failed or returned no moves"}

    sf_final  = sf.final()
    sf_final_cp = _cp(sf_final)

    # Per-depth comparison table
    depths_out: list[dict] = []
    for d in range(1, max_depth + 1):
        sf_d  = sf.at_depth(d)
        if sf_d is None:
            continue
        b80_d = b80.at_depth(d) if b80 else None
        b87_d = b87.at_depth(d) if b87 else None
        depths_out.append({
            "depth":     d,
            "sf_move":   sf_d.bestmove,
            "sf_cp":     _cp(sf_d),
            "sf_nodes":  sf_d.nodes,
            "b80_move":  b80_d.bestmove if b80_d else None,
            "b80_cp":    _cp(b80_d),
            "b80_nodes": b80_d.nodes if b80_d else None,
            "b80_agrees": (b80_d.bestmove == sf_d.bestmove) if b80_d else False,
            "b87_move":  b87_d.bestmove if b87_d else None,
            "b87_cp":    _cp(b87_d),
            "b87_nodes": b87_d.nodes if b87_d else None,
            "b87_agrees": (b87_d.bestmove == sf_d.bestmove) if b87_d else False,
        })

    b80_d1_cp    = _cp(b80.at_depth(1)) if b80 else None
    b87_d1_cp    = _cp(b87.at_depth(1)) if b87 else None
    b80_final_cp = _cp(b80.final()) if b80 else None
    b87_final_cp = _cp(b87.final()) if b87 else None

    return {
        "name":            name,
        "fen":             fen,
        "sf_final_move":   sf_final.bestmove if sf_final else None,
        "sf_final_cp":     sf_final_cp,
        "sf_final_nodes":  sf_final.nodes if sf_final else 0,
        "b80_final_move":  b80.final().bestmove if b80 and b80.final() else None,
        "b87_final_move":  b87.final().bestmove if b87 and b87.final() else None,
        "b80_final_cp":    b80_final_cp,
        "b87_final_cp":    b87_final_cp,
        "b80_final_nodes": b80.final().nodes if b80 and b80.final() else 0,
        "b87_final_nodes": b87.final().nodes if b87 and b87.final() else 0,
        "b80_agrees_sf":   (b80.final().bestmove == sf_final.bestmove)
                           if b80 and b80.final() and sf_final else False,
        "b87_agrees_sf":   (b87.final().bestmove == sf_final.bestmove)
                           if b87 and b87.final() and sf_final else False,
        "b80_eval_err":    abs(b80_final_cp - sf_final_cp)
                           if b80_final_cp is not None and sf_final_cp is not None else None,
        "b87_eval_err":    abs(b87_final_cp - sf_final_cp)
                           if b87_final_cp is not None and sf_final_cp is not None else None,
        "b80_fart_score":  _fart_score(b80_d1_cp, b80_final_cp, sf_final_cp),
        "b87_fart_score":  _fart_score(b87_d1_cp, b87_final_cp, sf_final_cp),
        "b80_static_err":  abs(b80_d1_cp - sf_final_cp)
                           if b80_d1_cp is not None and sf_final_cp is not None else None,
        "b87_static_err":  abs(b87_d1_cp - sf_final_cp)
                           if b87_d1_cp is not None and sf_final_cp is not None else None,
        "depths":          depths_out,
    }


# ── Printing ──────────────────────────────────────────────────────────────────
def _print_position_report(pos: dict, max_depth: int) -> None:
    if "error" in pos:
        print(f"  {red('ERROR')}: {pos['error']}")
        return

    print(f"\n  {'d':>2}  {'SF':8s}  {'SF_cp':>7}  "
          f"{'B80':8s}  {'B80_cp':>7}  {'=SF':>4}  "
          f"{'B87':8s}  {'B87_cp':>7}  {'=SF':>4}  "
          f"{'B80_nodes':>10}  {'B87_nodes':>10}")
    print(f"  {'─'*2}  {'─'*8}  {'─'*7}  "
          f"{'─'*8}  {'─'*7}  {'─'*4}  "
          f"{'─'*8}  {'─'*7}  {'─'*4}  "
          f"{'─'*10}  {'─'*10}")

    for row in pos.get("depths", []):
        d     = row["depth"]
        sf_m  = row["sf_move"] or "—"
        sf_c  = row["sf_cp"]
        b80_m = row.get("b80_move") or "—"
        b87_m = row.get("b87_move") or "—"
        b80_c = row.get("b80_cp")
        b87_c = row.get("b87_cp")
        b80_a = row.get("b80_agrees", False)
        b87_a = row.get("b87_agrees", False)
        b80_n = row.get("b80_nodes") or 0
        b87_n = row.get("b87_nodes") or 0

        sf_s   = f"{sf_m:<8}"
        scp_s  = f"{sf_c/100:+6.2f}" if sf_c is not None else "    —  "
        b80_s  = (green if b80_a else red)(f"{b80_m:<8}")
        b87_s  = (green if b87_a else red)(f"{b87_m:<8}")
        b80_cs = f"{b80_c/100:+6.2f}" if b80_c is not None else "    —  "
        b87_cs = f"{b87_c/100:+6.2f}" if b87_c is not None else "    —  "
        a80    = green("  ✓") if b80_a else red("  ✗")
        a87    = green("  ✓") if b87_a else red("  ✗")

        print(f"  {d:>2}  {sf_s}  {scp_s}  "
              f"{b80_s}  {b80_cs}  {a80}  "
              f"{b87_s}  {b87_cs}  {a87}  "
              f"{b80_n:>10,}  {b87_n:>10,}")

    sf_n   = pos.get("sf_final_nodes", 1) or 1
    b80_n  = pos.get("b80_final_nodes", 0) or 0
    b87_n  = pos.get("b87_final_nodes", 0) or 0
    err80  = pos.get("b80_eval_err")
    err87  = pos.get("b87_eval_err")
    se80   = pos.get("b80_static_err")
    se87   = pos.get("b87_static_err")
    fs80   = pos.get("b80_fart_score")
    fs87   = pos.get("b87_fart_score")
    fa80   = green("✓") if pos.get("b80_agrees_sf") else red("✗")
    fa87   = green("✓") if pos.get("b87_agrees_sf") else red("✗")

    print(f"\n  Final move: SF={pos.get('sf_final_move')}  "
          f"b80={pos.get('b80_final_move')} {fa80}  "
          f"b87={pos.get('b87_final_move')} {fa87}")
    print(f"  Static err: b80={se80}cp  b87={se87}cp  (vs SF depth-{_MAX_DEPTH} eval)")
    print(f"  Search err: b80={err80}cp  b87={err87}cp")
    if fs80 is not None and fs87 is not None:
        fs80_s = (red if fs80 > 0.7 else (yellow if fs80 > 0.4 else green))(f"{fs80:.2f}")
        fs87_s = (red if fs87 > 0.7 else (yellow if fs87 > 0.4 else green))(f"{fs87:.2f}")
        print(f"  Fart score: b80={fs80_s}  b87={fs87_s}  (1.0=stayed at static, 0.0=reached SF)")
    print(f"  Nodes/SF:   b80={b80_n/sf_n:.2f}x  b87={b87_n/sf_n:.2f}x")


def _print_aggregate(positions: list[dict]) -> None:
    valid = [p for p in positions if "error" not in p]
    n = len(valid)
    if n == 0:
        print("No valid results.")
        return

    b80_agree = sum(1 for p in valid if p.get("b80_agrees_sf"))
    b87_agree = sum(1 for p in valid if p.get("b87_agrees_sf"))

    # Per-depth agreement accumulation
    from collections import defaultdict
    d_b80: dict[int, list[bool]] = defaultdict(list)
    d_b87: dict[int, list[bool]] = defaultdict(list)
    for p in valid:
        for row in p.get("depths", []):
            d_b80[row["depth"]].append(row.get("b80_agrees", False))
            d_b87[row["depth"]].append(row.get("b87_agrees", False))

    # Eval & fart stats
    errs80  = [p["b80_eval_err"]    for p in valid if p.get("b80_eval_err") is not None]
    errs87  = [p["b87_eval_err"]    for p in valid if p.get("b87_eval_err") is not None]
    se80    = [p["b80_static_err"]  for p in valid if p.get("b80_static_err") is not None]
    se87    = [p["b87_static_err"]  for p in valid if p.get("b87_static_err") is not None]
    fart80  = [p["b80_fart_score"]  for p in valid if p.get("b80_fart_score") is not None]
    fart87  = [p["b87_fart_score"]  for p in valid if p.get("b87_fart_score") is not None]

    def _mean(xs): return sum(xs) / len(xs) if xs else None

    mae80  = _mean(errs80)
    mae87  = _mean(errs87)
    mse80  = _mean(se80)
    mse87  = _mean(se87)
    mf80   = _mean(fart80)
    mf87   = _mean(fart87)

    # Node ratios
    nr80 = [p["b80_final_nodes"] / p["sf_final_nodes"]
            for p in valid
            if p.get("sf_final_nodes") and p.get("b80_final_nodes")]
    nr87 = [p["b87_final_nodes"] / p["sf_final_nodes"]
            for p in valid
            if p.get("sf_final_nodes") and p.get("b87_final_nodes")]
    mnr80 = _mean(nr80)
    mnr87 = _mean(nr87)

    W = 74
    print("\n" + "=" * W)
    print(bold("  AGGREGATE SUMMARY"))
    print("=" * W)
    print(f"  Positions analyzed: {n}")
    print()

    pct80 = 100 * b80_agree / n
    pct87 = 100 * b87_agree / n
    delta = pct80 - pct87
    winner = (green("build-80 ↑") if delta > 2 else
              (red("build-87 ↑") if delta < -2 else yellow("tied")))
    print(f"  {'FINAL-DEPTH BESTMOVE AGREEMENT WITH SF'}")
    print(f"  {'─'*38}")
    print(f"  build-80:  {b80_agree:2d}/{n}  ({pct80:.1f}%)")
    print(f"  build-87:  {b87_agree:2d}/{n}  ({pct87:.1f}%)")
    print(f"  Δ(80-87):  {delta:+.1f}pp  → {winner}")
    print()

    # Per-depth table
    all_depths = sorted(set(d_b80) | set(d_b87))
    if all_depths:
        print(f"  {'depth':>5}  {'b80 agree':>12}  {'b87 agree':>12}  {'Δ(80-87)':>10}")
        print(f"  {'─'*5}  {'─'*12}  {'─'*12}  {'─'*10}")
        for d in all_depths:
            r80 = d_b80.get(d, [])
            r87 = d_b87.get(d, [])
            p80 = 100 * sum(r80) / len(r80) if r80 else None
            p87 = 100 * sum(r87) / len(r87) if r87 else None
            p80s = f"{p80:.1f}%" if p80 is not None else "   —   "
            p87s = f"{p87:.1f}%" if p87 is not None else "   —   "
            delta_d = (p80 or 0) - (p87 or 0)
            ds = f"{delta_d:+.1f}pp"
            dc = (green if delta_d > 2 else (red if delta_d < -2 else yellow))(ds)
            print(f"  {d:>5}  {p80s:>12}  {p87s:>12}  {dc:>10}")
        print()

    # Eval & fart table
    print(f"  {'Metric':<40}  {'build-80':>10}  {'build-87':>10}")
    print(f"  {'─'*40}  {'─'*10}  {'─'*10}")
    if mse80 is not None:
        print(f"  {'Static eval MAE vs SF (cp)':<40}  {mse80:>10.1f}  {mse87:>10.1f}")
    if mae80 is not None:
        w = (green("b80 ↓") if mae80 < mae87 else red("b87 ↓")) if mae80 != mae87 else yellow("tied")
        print(f"  {'Search eval MAE vs SF (cp)':<40}  {mae80:>10.1f}  {mae87:>10.1f}  {w}")
    if mf80 is not None:
        w = (green("b80 lower") if mf80 < mf87 else red("b87 more fart")) if abs(mf80-mf87) > 0.02 else yellow("tied")
        print(f"  {'Own-fart score (0=SF, 1=static, >1=worse)':<40}  {mf80:>10.3f}  {mf87:>10.3f}  {w}")
    if mnr80 is not None:
        w = (green("b80 broader") if mnr80 > mnr87 else yellow("b87 broader"))
        print(f"  {'Mean nodes ratio vs SF':<40}  {mnr80:>10.3f}  {mnr87:>10.3f}  {w}")
    print("=" * W)


# ── Main ──────────────────────────────────────────────────────────────────────
_MAX_DEPTH = 14  # module-level for use in formatting


def main() -> None:
    global _MAX_DEPTH

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf",        default=None, help="Path to Stockfish binary")
    ap.add_argument("--build80",   default=None, help="Path to build-80 redux-hce.exe")
    ap.add_argument("--build87",   default=None, help="Path to build-87 redux-hce.exe (default: current build)")
    ap.add_argument("--positions", default=str(_ROOT / _SUITE_DEFAULT),
                    help="Position suite file (name|fen, one per line)")
    ap.add_argument("--depth",     type=int, default=14,
                    help="Max depth per engine (default: 14)")
    ap.add_argument("--json",      default=None, metavar="OUT",
                    help="Write raw results to JSON file")
    ap.add_argument("--limit",     type=int, default=0,
                    help="Only run first N positions (0 = all)")
    ap.add_argument("--verbose",   action="store_true",
                    help="Print per-position depth tables")
    args = ap.parse_args()

    _MAX_DEPTH = args.depth

    # Resolve paths
    sf_path  = args.sf    or _find(_SF_CANDIDATES,  "Stockfish")
    b80_path = args.build80 or _find(_B80_CANDIDATES, "build-80")
    b87_path = args.build87 or _find(_B87_CANDIDATES, "build-87")

    # Load positions
    positions: list[tuple[str, str]] = []
    with open(args.positions) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, fen = line.split("|", 1)
                positions.append((name.strip(), fen.strip()))
    if args.limit:
        positions = positions[:args.limit]

    print(f"\n{'='*74}")
    print(bold("  TREE COMPARE"))
    print(f"{'='*74}")
    print(f"  SF       : {sf_path}")
    print(f"  build-80 : {b80_path}")
    print(f"  build-87 : {b87_path}")
    print(f"  Positions: {len(positions)}")
    print(f"  Depth    : {args.depth}")
    print(f"{'='*74}\n")

    all_results: list[dict] = []

    for i, (name, fen) in enumerate(positions, 1):
        print(f"\n[{i:2d}/{len(positions)}] {bold(name)}")
        print(f"  FEN: {dim(fen)}")

        result = analyse_position(name, fen, args.depth, sf_path, b80_path, b87_path)
        all_results.append(result)

        if args.verbose:
            _print_position_report(result, args.depth)
        else:
            # One-line summary
            if "error" in result:
                print(f"  {red('ERROR')}: {result['error']}")
            else:
                fa80 = green("✓") if result.get("b80_agrees_sf") else red("✗")
                fa87 = green("✓") if result.get("b87_agrees_sf") else red("✗")
                fs80 = result.get("b80_fart_score")
                fs87 = result.get("b87_fart_score")
                fs80_s = f"{fs80:.2f}" if fs80 is not None else "n/a"
                fs87_s = f"{fs87:.2f}" if fs87 is not None else "n/a"
                print(f"  SF={result.get('sf_final_move')}  "
                      f"b80={result.get('b80_final_move')} {fa80}  "
                      f"b87={result.get('b87_final_move')} {fa87}  "
                      f"err b80={result.get('b80_eval_err')}cp b87={result.get('b87_eval_err')}cp  "
                      f"fart b80={fs80_s} b87={fs87_s}")

    _print_aggregate(all_results)

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({"depth": args.depth, "positions": all_results}, f, indent=2)
        print(f"\n  Results written to: {out_path}")


if __name__ == "__main__":
    main()
