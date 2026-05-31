#!/usr/bin/env python3
"""
eval_accuracy.py — Quantify static and search eval accuracy vs Stockfish ground truth.

For each position in the suite:
  1. Run Stockfish at depth 25 → ground-truth eval
  2. Run --base and --test engines at depth 1 (static eval) and depth 12
  3. Report: Pearson r, MAE, % within 50cp, % sign-correct

Key metrics:
  • static_mae — mean absolute error of d1 eval vs SF. Shows raw eval quality.
  • search_mae — mean absolute error of d12 eval vs SF. Shows search benefit.
  • fart_pct   — % of positions where d12 eval drifted AWAY from SF vs d1.
                 i.e. search made the eval WORSE. That's the "own farts" signal.
  • pearson_r  — correlation between our evals and SF. <0.85 = structural gap.
  • sign_err   — % of positions where we evaluate the position with the wrong sign
                 (we think we're up when SF says we're down, or vice versa).

Usage
-----
    python tools/analysis/eval_accuracy.py
    python tools/analysis/eval_accuracy.py --sf-depth 25 --depth 12
    python tools/analysis/eval_accuracy.py \\
        --test bot/engine/redux-hce.exe \\
        --base archives/build-80/redux-hce.exe \\
        --sf engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe \\
        --positions data/benchmarks/search/nps_core.txt \\
        --json results/analysis/eval_accuracy.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
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

# ── Default paths ─────────────────────────────────────────────────────────────
_SF_CANDIDATES  = [
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


def _path_label(path: str) -> str:
    """Derive a short display label from a binary path.

    archives/build-89/redux-hce.exe  →  'build-89'
    bot/engine/redux-hce.exe         →  'current'
    build/redux-hce.exe              →  'current'
    """
    p = Path(path)
    parent = p.parent.name
    if parent.startswith("build-"):
        return parent
    if parent in ("engine", "bin", "build", ""):
        return "current"
    return parent


# ── Single-depth engine query ─────────────────────────────────────────────────
def _get_eval(engine_path: str, fen: str, depth: int,
              extra_options: dict | None = None) -> Optional[int]:
    """
    Run engine on fen to given depth. Return final score as centipawns
    from side-to-move perspective (±10000 for mate). Returns None on failure.
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
    except Exception:
        return None

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
    _send(f"go depth {depth}")

    done.wait(timeout=60)
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

    # Extract score from last info line with a score
    score_cp: Optional[int] = None
    for line in reversed(collected):
        parts = line.split()
        if not parts or parts[0] != "info":
            continue
        if len(parts) >= 2 and parts[1] == "string":
            continue
        i = 1
        while i < len(parts):
            if parts[i] == "score" and i + 2 < len(parts):
                if parts[i + 1] == "cp":
                    try:
                        score_cp = int(parts[i + 2])
                    except ValueError:
                        pass
                    break
                elif parts[i + 1] == "mate":
                    try:
                        m = int(parts[i + 2])
                        score_cp = 10000 if m > 0 else -10000
                    except ValueError:
                        pass
                    break
            i += 1
        if score_cp is not None:
            break

    return score_cp


# ── Statistics ─────────────────────────────────────────────────────────────────
def pearson_r(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx   = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy   = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return cov / (sx * sy)


def _cap(v: Optional[int], cap: int = 2000) -> Optional[int]:
    """Cap extreme mate/winning scores to keep stats meaningful."""
    if v is None:
        return None
    return max(-cap, min(cap, v))


# ── Per-position probe ────────────────────────────────────────────────────────
def probe_position(name: str, fen: str,
                   sf_path: str, base_path: str, test_path: str,
                   sf_depth: int, our_depth: int) -> dict:
    """Get SF, base (d1+d12), test (d1+d12) evals for one position."""
    results: dict[str, Optional[int]] = {}

    jobs = [
        ("sf",        sf_path,   sf_depth,  None),
        ("base_d1",   base_path, 1,         None),
        ("base_d12",  base_path, our_depth, None),
        ("test_d1",   test_path, 1,         {"UseNNUE": "false"}),
        ("test_d12",  test_path, our_depth, {"UseNNUE": "false"}),
    ]

    # Run sequentially to avoid multiple heavy SF instances in parallel (crashes)
    for key, path, depth, opts in jobs:
        results[key] = _get_eval(path, fen, depth, opts)

    sf_cp    = _cap(results.get("sf"))
    base_d1  = _cap(results.get("base_d1"))
    base_d12 = _cap(results.get("base_d12"))
    test_d1  = _cap(results.get("test_d1"))
    test_d12 = _cap(results.get("test_d12"))

    def _err(v: Optional[int], gt: Optional[int]) -> Optional[int]:
        if v is None or gt is None:
            return None
        return abs(v - gt)

    def _fart(d1: Optional[int], dn: Optional[int], gt: Optional[int]) -> Optional[float]:
        """Own-fart score: 0 = improved to SF, 1 = stayed at static, >1 = got worse."""
        if d1 is None or dn is None or gt is None:
            return None
        d1_err = abs(d1 - gt)
        if d1_err < 5:
            return None
        return abs(dn - gt) / d1_err

    def _sign_wrong(v: Optional[int], gt: Optional[int]) -> Optional[bool]:
        if v is None or gt is None:
            return None
        return (v > 10) != (gt > 10) and abs(gt) > 10

    return {
        "name":             name,
        "fen":              fen,
        "sf_cp":            sf_cp,
        "base_d1":          base_d1,
        "base_d12":         base_d12,
        "test_d1":          test_d1,
        "test_d12":         test_d12,
        # errors
        "base_static_err":  _err(base_d1,  sf_cp),
        "test_static_err":  _err(test_d1,  sf_cp),
        "base_search_err":  _err(base_d12, sf_cp),
        "test_search_err":  _err(test_d12, sf_cp),
        # own-fart scores
        "base_fart":        _fart(base_d1, base_d12, sf_cp),
        "test_fart":        _fart(test_d1, test_d12, sf_cp),
        # sign errors
        "base_d1_sign_wrong":  _sign_wrong(base_d1,  sf_cp),
        "base_d12_sign_wrong": _sign_wrong(base_d12, sf_cp),
        "test_d1_sign_wrong":  _sign_wrong(test_d1,  sf_cp),
        "test_d12_sign_wrong": _sign_wrong(test_d12, sf_cp),
    }


# ── Aggregate + report ────────────────────────────────────────────────────────
def _report(positions: list[dict], our_depth: int,
            base_label: str = "base", test_label: str = "test") -> None:
    valid = [p for p in positions if "sf_cp" in p and p["sf_cp"] is not None]
    n = len(valid)
    if n == 0:
        print("No valid results.")
        return

    def _collect(key: str) -> list[float]:
        return [p[key] for p in valid if p.get(key) is not None]

    def _mean(xs: list) -> Optional[float]:
        return sum(xs) / len(xs) if xs else None

    def _pct_within(errs: list[float], threshold: int) -> Optional[float]:
        if not errs:
            return None
        return 100 * sum(1 for e in errs if e <= threshold) / len(errs)

    def _pct_true(bools: list) -> Optional[float]:
        b = [x for x in bools if x is not None]
        return 100 * sum(b) / len(b) if b else None

    sf_vals    = [p["sf_cp"] for p in valid]
    base_d1_v  = _collect("base_d1")
    base_d12_v = _collect("base_d12")
    test_d1_v  = _collect("test_d1")
    test_d12_v = _collect("test_d12")

    # Pearson r (static eval vs SF)
    r_base_d1  = pearson_r([p["sf_cp"] for p in valid if p.get("base_d1") is not None],
                            [p["base_d1"] for p in valid if p.get("base_d1") is not None])
    r_base_d12 = pearson_r([p["sf_cp"] for p in valid if p.get("base_d12") is not None],
                            [p["base_d12"] for p in valid if p.get("base_d12") is not None])
    r_test_d1  = pearson_r([p["sf_cp"] for p in valid if p.get("test_d1") is not None],
                            [p["test_d1"] for p in valid if p.get("test_d1") is not None])
    r_test_d12 = pearson_r([p["sf_cp"] for p in valid if p.get("test_d12") is not None],
                            [p["test_d12"] for p in valid if p.get("test_d12") is not None])

    errs_base_d1  = _collect("base_static_err")
    errs_base_d12 = _collect("base_search_err")
    errs_test_d1  = _collect("test_static_err")
    errs_test_d12 = _collect("test_search_err")

    fart_base  = _collect("base_fart")
    fart_test  = _collect("test_fart")
    # Percentage where fart_score > 1 (search made eval WORSE)
    fart_base_pct_worse     = 100 * sum(1 for f in fart_base if f > 1.0) / len(fart_base) if fart_base else None
    fart_test_pct_worse     = 100 * sum(1 for f in fart_test if f > 1.0) / len(fart_test) if fart_test else None
    fart_base_pct_improving = 100 * sum(1 for f in fart_base if f < 0.5) / len(fart_base) if fart_base else None
    fart_test_pct_improving = 100 * sum(1 for f in fart_test if f < 0.5) / len(fart_test) if fart_test else None

    sign_base_d1  = [p.get("base_d1_sign_wrong")  for p in valid]
    sign_base_d12 = [p.get("base_d12_sign_wrong") for p in valid]
    sign_test_d1  = [p.get("test_d1_sign_wrong")  for p in valid]
    sign_test_d12 = [p.get("test_d12_sign_wrong") for p in valid]

    W = 72
    print("\n" + "=" * W)
    print(bold("  EVAL ACCURACY vs STOCKFISH"))
    print("=" * W)
    print(f"  Positions: {n}  |  Depth: d1 (static) vs d{our_depth} (search)  |  SF: depth 25")
    print()

    # Per-position summary table
    ba_d1  = f"{base_label}_d1";  ba_d12 = f"{base_label}_d12"
    te_d1  = f"{test_label}_d1";  te_d12 = f"{test_label}_d12"
    print(f"  {'Position':<24}  {'SF':>6}  "
          f"{ba_d1:>10}  {ba_d12:>10}  "
          f"{te_d1:>10}  {te_d12:>10}  "
          f"{'fart_base':>10}  {'fart_test':>10}")
    print(f"  {'─'*24}  {'─'*6}  "
          f"{'─'*10}  {'─'*10}  "
          f"{'─'*10}  {'─'*10}  "
          f"{'─'*10}  {'─'*10}")
    for p in valid:
        sf_s       = f"{p['sf_cp']:+5d}"    if p.get("sf_cp")   is not None else "  —  "
        base_d1_s  = f"{p['base_d1']:+5d}"  if p.get("base_d1")  is not None else "  —  "
        base_d12_s = f"{p['base_d12']:+5d}" if p.get("base_d12") is not None else "  —  "
        test_d1_s  = f"{p['test_d1']:+5d}"  if p.get("test_d1")  is not None else "  —  "
        test_d12_s = f"{p['test_d12']:+5d}" if p.get("test_d12") is not None else "  —  "
        fs_base = p.get("base_fart")
        fs_test = p.get("test_fart")
        fs_base_s = (red if fs_base is not None and fs_base > 0.8 else
                     (green if fs_base is not None and fs_base < 0.4 else yellow))(
                         f"{fs_base:.2f}" if fs_base is not None else "  —  ")
        fs_test_s = (red if fs_test is not None and fs_test > 0.8 else
                     (green if fs_test is not None and fs_test < 0.4 else yellow))(
                         f"{fs_test:.2f}" if fs_test is not None else "  —  ")
        print(f"  {p['name']:<24}  {sf_s:>6}  "
              f"{base_d1_s:>10}  {base_d12_s:>10}  "
              f"{test_d1_s:>10}  {test_d12_s:>10}  "
              f"{fs_base_s:>10}  {fs_test_s:>10}")

    print()
    # Aggregate stats table
    def _fmt(v: Optional[float], fmt: str = ".1f") -> str:
        return f"{v:{fmt}}" if v is not None else "   —   "

    def _fmtr(v: Optional[float]) -> str:
        if v is None:
            return "   —   "
        return (green if v > 0.90 else (yellow if v > 0.80 else red))(f"{v:.4f}")

    def _fmte(v: Optional[float]) -> str:
        if v is None:
            return "   —   "
        return (green if v < 50 else (yellow if v < 100 else red))(f"{v:.1f}cp")

    print(f"  {'Metric':<42}  {base_label:>12}  {test_label:>12}")
    print(f"  {'─'*42}  {'─'*12}  {'─'*12}")

    # Pearson r
    print(f"  {'Pearson r (d1 static vs SF)':<42}  {_fmtr(r_base_d1):>12}  {_fmtr(r_test_d1):>12}")
    print(f"  {'Pearson r (d12 search vs SF)':<42}  {_fmtr(r_base_d12):>12}  {_fmtr(r_test_d12):>12}")

    # MAE
    mae_base_d1  = _mean(errs_base_d1)
    mae_base_d12 = _mean(errs_base_d12)
    mae_test_d1  = _mean(errs_test_d1)
    mae_test_d12 = _mean(errs_test_d12)
    print(f"  {'MAE d1 static (cp)':<42}  {_fmte(mae_base_d1):>12}  {_fmte(mae_test_d1):>12}")
    print(f"  {'MAE d12 search (cp)':<42}  {_fmte(mae_base_d12):>12}  {_fmte(mae_test_d12):>12}")

    # % within 50cp
    w50_base_d1  = _pct_within(errs_base_d1,  50)
    w50_base_d12 = _pct_within(errs_base_d12, 50)
    w50_test_d1  = _pct_within(errs_test_d1,  50)
    w50_test_d12 = _pct_within(errs_test_d12, 50)
    print(f"  {'% within 50cp d1':<42}  {_fmt(w50_base_d1):>12}  {_fmt(w50_test_d1):>12}")
    print(f"  {'% within 50cp d12':<42}  {_fmt(w50_base_d12):>12}  {_fmt(w50_test_d12):>12}")

    # Sign error
    se_base_d1  = _pct_true(sign_base_d1)
    se_base_d12 = _pct_true(sign_base_d12)
    se_test_d1  = _pct_true(sign_test_d1)
    se_test_d12 = _pct_true(sign_test_d12)
    print(f"  {'Sign error % d1':<42}  {_fmt(se_base_d1):>12}  {_fmt(se_test_d1):>12}")
    print(f"  {'Sign error % d12':<42}  {_fmt(se_base_d12):>12}  {_fmt(se_test_d12):>12}")

    # Fart stats
    mf_base = _mean(fart_base)
    mf_test = _mean(fart_test)
    print(f"  {'Mean own-fart score (0=SF, 1=static)':<42}  {_fmt(mf_base, '.3f'):>12}  {_fmt(mf_test, '.3f'):>12}")
    if fart_base_pct_worse is not None:
        print(f"  {'% positions where search made eval WORSE':<42}  "
              f"{_fmt(fart_base_pct_worse):>12}  {_fmt(fart_test_pct_worse):>12}")
    if fart_base_pct_improving is not None:
        print(f"  {'% positions where fart < 0.5 (search helped)':<42}  "
              f"{_fmt(fart_base_pct_improving):>12}  {_fmt(fart_test_pct_improving):>12}")

    print()
    # Interpretation
    print("  INTERPRETATION")
    print(f"  {'─'*70}")
    if r_base_d1 is not None and r_base_d1 < 0.85:
        print(f"  ⚠  Pearson r={r_base_d1:.3f} < 0.85 ({base_label}): static eval has structural gaps")
    if mf_base is not None and mf_base > 0.6:
        print(f"  ⚠  Fart score={mf_base:.2f} > 0.6 ({base_label}): search not correcting eval errors")
    if mf_test is not None and mf_test > 0.6:
        print(f"  ⚠  Fart score={mf_test:.2f} > 0.6 ({test_label}): search not correcting eval errors")
    if mae_base_d12 is not None and mae_base_d1 is not None and mae_base_d12 > mae_base_d1 * 0.8:
        print(f"  ⚠  Search barely reduces MAE (d1={mae_base_d1:.0f}cp → d12={mae_base_d12:.0f}cp for {base_label})")
    if r_test_d12 is not None and r_base_d12 is not None:
        if r_test_d12 < r_base_d12:
            print(f"  ⚠  {test_label} Pearson r at d12 ({r_test_d12:.3f}) < {base_label} ({r_base_d12:.3f})")
            print(f"     → {test_label}'s search is MORE eval-dependent, not less")
    print("=" * W)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf",    default=None)
    ap.add_argument("--base",  default=None, metavar="PATH",
                    help="Reference/baseline engine binary (default: archives/build-80/redux-hce.exe)")
    ap.add_argument("--test",  default=None, metavar="PATH",
                    help="Engine under test (default: bot/engine/redux-hce.exe)")
    ap.add_argument("--positions", default=str(_ROOT / _SUITE_DEFAULT))
    ap.add_argument("--sf-depth",  type=int, default=25)
    ap.add_argument("--depth",     type=int, default=12,
                    help="Our engine search depth (default: 12)")
    ap.add_argument("--json",      default=None, metavar="OUT")
    ap.add_argument("--limit",     type=int, default=0)
    args = ap.parse_args()

    sf_path   = args.sf   or _find(_SF_CANDIDATES,  "Stockfish")
    base_path = args.base or _find(_B80_CANDIDATES, "base engine")
    test_path = args.test or _find(_B87_CANDIDATES, "test engine")
    base_label = _path_label(base_path)
    test_label = _path_label(test_path)

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

    print(f"\n{'='*72}")
    print(bold("  EVAL ACCURACY"))
    print(f"{'='*72}")
    print(f"  SF (d{args.sf_depth})           : {sf_path}")
    print(f"  {base_label} (d1/d{args.depth}) : {base_path}")
    print(f"  {test_label} (d1/d{args.depth}) : {test_path}")
    print(f"  Positions          : {len(positions)}")
    print(f"{'='*72}\n")

    all_results: list[dict] = []
    for i, (name, fen) in enumerate(positions, 1):
        print(f"  [{i:2d}/{len(positions)}] {name} ...", end=" ", flush=True)
        result = probe_position(name, fen, sf_path, base_path, test_path,
                                args.sf_depth, args.depth)
        all_results.append(result)
        sf_v      = result.get("sf_cp")
        base_s    = result.get("base_d12")
        test_s    = result.get("test_d12")
        err_base  = result.get("base_search_err")
        err_test  = result.get("test_search_err")
        print(f"SF={sf_v}  {base_label}={base_s}(Δ{err_base})  {test_label}={test_s}(Δ{err_test})")

    _report(all_results, args.depth, base_label, test_label)

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({
                "sf_depth": args.sf_depth,
                "our_depth": args.depth,
                "positions": all_results,
            }, f, indent=2)
        print(f"\n  Results written to: {out_path}")


if __name__ == "__main__":
    main()
