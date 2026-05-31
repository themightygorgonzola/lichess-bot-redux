"""
compare_nnue_hce.py  —  Compare NNUE vs HCE evaluation over diagnosed positions.

Loads an existing *_diagnosed.json (which contains SF bestmoves) and re-probes
each anchor position with both NNUE and HCE in parallel, then reports which
evaluator agrees more often with Stockfish per category.

Usage:
    python tools/compare_nnue_hce.py
    python tools/compare_nnue_hce.py --movetime 3000
    python tools/compare_nnue_hce.py \\
        --input  results/analysis/2026-03_diagnosed.json \\
        --engine bot/engine/redux-nnue.exe \\
        --movetime 2000 \\
        --threads 2

Both NNUE and HCE are run using the same redux-nnue.exe binary.
NNUE mode: no extra setoption (UseNNUE defaults to true, nn.bin auto-loaded).
HCE mode:  setoption name UseNNUE value false
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_TOOLS_DIR = str(Path(__file__).parent.parent)  # tools/ root for engine_config import
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

try:
    from engine_config import ENGINE_PATH as _DEFAULT_ENGINE, NNUE_PATH as _DEFAULT_NNUE
except ImportError:
    _DEFAULT_ENGINE = None
    _DEFAULT_NNUE   = None

_DEFAULT_INPUT = Path("results/analysis/2026-03_diagnosed.json")

# ---------------------------------------------------------------------------
# Engine probe
# ---------------------------------------------------------------------------

_INFO_RE = re.compile(
    r'info\s+depth\s+(\d+)'
    r'(?:.*?score\s+(cp|mate)\s+([-\d]+))?'
    r'(?:.*?pv\s+(.+))?'
)


def _probe(engine_path: str, fen: str, movetime_ms: int, options: dict) -> str | None:
    """Run engine on position and return bestmove string (or None on error)."""
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
        return None

    lines: list[str] = []
    done = threading.Event()

    def _reader():
        for line in proc.stdout:
            lines.append(line.rstrip())
            if "bestmove" in line:
                done.set()

    reader_t = threading.Thread(target=_reader, daemon=True)
    reader_t.start()

    def _send(msg: str):
        try:
            proc.stdin.write(msg + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass

    _send("uci")
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("uciok" in l for l in lines):
            break
        time.sleep(0.05)

    for name, value in options.items():
        _send(f"setoption name {name} value {value}")
    _send("isready")
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("readyok" in l for l in lines):
            break
        time.sleep(0.05)

    _send("ucinewgame")
    _send(f"position fen {fen}")
    _send(f"go movetime {movetime_ms}")

    done.wait(timeout=movetime_ms / 1000 + 5)
    _send("quit")
    reader_t.join(timeout=2)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

    for line in reversed(lines):
        if line.startswith("bestmove"):
            parts = line.split()
            if len(parts) >= 2 and parts[1] != "(none)":
                return parts[1]
    return None


def _probe_both(engine_path: str, nnue_path: str | None,
                fen: str, movetime_ms: int, threads: int, hash_mb: int,
                engine2_path: str | None = None) -> tuple[str | None, str | None]:
    """
    Return (e1_bestmove, e2_bestmove) probed in parallel.

    Default mode (engine2_path=None): e1=NNUE mode, e2=HCE mode, same binary.
    engine2 mode: e1=engine_path default opts, e2=engine2_path default opts.
    This lets you compare any two engine binaries (e.g. build-80 vs build-87)
    against the same SF reference without changing the rest of the logic.
    """
    base_opts = {"Threads": threads, "Hash": hash_mb}
    if engine2_path:
        # Two-binary mode: both run with their default options (HCE implied by
        # binary choice — pass UseNNUE=false explicitly since archive builds
        # may or may not have NNUE loaded)
        e1_opts = {**base_opts, "UseNNUE": "false"}
        e2_opts = {**base_opts, "UseNNUE": "false"}
        e1_path = engine_path
        e2_path = engine2_path
    else:
        nnue_opts = dict(base_opts)
        if nnue_path:
            nnue_opts["EvalFile"] = nnue_path
        e1_opts = nnue_opts
        e2_opts = {**base_opts, "UseNNUE": "false"}
        e1_path = engine_path
        e2_path = engine_path

    nnue_result: list[str | None] = [None]
    hce_result:  list[str | None] = [None]

    def _run_nnue():
        nnue_result[0] = _probe(e1_path, fen, movetime_ms, e1_opts)

    def _run_hce():
        hce_result[0] = _probe(e2_path, fen, movetime_ms, e2_opts)

    t1 = threading.Thread(target=_run_nnue, daemon=True)
    t2 = threading.Thread(target=_run_hce,  daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()

    return nnue_result[0], hce_result[0]


# ---------------------------------------------------------------------------
# Results aggregation
# ---------------------------------------------------------------------------

def _depth_bucket(row: dict) -> str:
    d = row.get("first_sf_depth")
    if d is None:
        return "never"
    if d <= 10:  return "d1-10"
    if d <= 15:  return "d11-15"
    if d <= 20:  return "d16-20"
    if d <= 25:  return "d21-25"
    return "d26+"


def _cats(row: dict) -> list[str]:
    return row.get("categories") or ["positional"]


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "  —  "
    return f"{100 * num / den:5.1f}%"


def _delta(nnue: int, hce: int, n: int) -> str:
    if n == 0:
        return "    —"
    d = 100 * (nnue - hce) / n
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:4.1f}pp"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Compare NNUE vs HCE on diagnosed positions")
    ap.add_argument("--input",    default=str(_DEFAULT_INPUT),
                    help="Diagnosed JSON with SF bestmoves (default: %(default)s)")
    ap.add_argument("--engine",   default=_DEFAULT_ENGINE,
                    help="Path to redux-nnue.exe (default: auto-discovered)")
    ap.add_argument("--nnue",     default=_DEFAULT_NNUE,
                    help="Path to nn.bin (default: auto-discovered next to engine)")
    ap.add_argument("--engine2",  default=None,
                    help="Optional second engine binary. When set, compares engine vs "
                         "engine2 (both in HCE mode) instead of NNUE vs HCE mode. "
                         "Example: --engine build/redux-hce.exe --engine2 archives/build-80/redux-hce.exe")
    ap.add_argument("--label1",   default=None,
                    help="Label for engine  (default: 'NNUE' or basename of --engine)")
    ap.add_argument("--label2",   default=None,
                    help="Label for engine2 (default: 'HCE'  or basename of --engine2)")
    ap.add_argument("--movetime", type=int, default=2000,
                    help="Movetime per position per engine in ms (default: 2000)")
    ap.add_argument("--threads",  type=int, default=1,
                    help="UCI Threads for each engine instance (default: 1)")
    ap.add_argument("--hash",     type=int, default=64,
                    help="UCI Hash in MB per engine instance (default: 64)")
    ap.add_argument("--workers",  type=int, default=4,
                    help="Parallel positions (each uses 2 engine processes, default: 4)")
    ap.add_argument("--limit",    type=int, default=0,
                    help="Only probe first N positions (0 = all, for quick smoke tests)")
    ap.add_argument("--out",      default=None,
                    help="Write raw results JSON to this path (optional)")
    args = ap.parse_args()

    # Resolve labels
    _label1 = args.label1 or (Path(args.engine).stem if args.engine2 else "NNUE")
    _label2 = args.label2 or (Path(args.engine2).stem if args.engine2 else "HCE")

    if not args.engine or not Path(args.engine).is_file():
        sys.exit(f"[compare] Engine not found: {args.engine}\n"
                 "  Build with: cmake --build build --target redux-nnue\n"
                 "  Or pass --engine /path/to/redux-nnue.exe")

    # Load diagnosed positions (anchors only, must have sf_bestmove)
    with open(args.input) as f:
        raw = json.load(f)

    rows = [
        r for r in raw.get("results", [])
        if r.get("window_role", "anchor") == "anchor"
        and "error" not in r
        and r.get("sf_bestmove")
    ]

    if not rows:
        sys.exit("[compare] No anchor rows with sf_bestmove found in input file.")

    if args.limit:
        rows = rows[: args.limit]

    print(f"\n{'='*72}")
    if args.engine2:
        print(f"  {_label1} vs {_label2} comparison (engine2 mode)")
        print(f"  Engine 1 : {args.engine}")
        print(f"  Engine 2 : {args.engine2}")
    else:
        print(f"  NNUE vs HCE comparison")
        print(f"  Engine   : {args.engine}")
        print(f"  NNUE     : {args.nnue or '(auto-discover next to binary)'}")
    print(f"  Positions: {len(rows)}")
    print(f"  Movetime : {args.movetime} ms per engine per position")
    print(f"  Workers  : {args.workers} parallel (each spawns 2 engine procs)")
    print(f"{'='*72}\n")

    # ---- probe loop -------------------------------------------------------

    results: list[dict] = []  # {row, nnue_move, hce_move, nnue_agrees, hce_agrees}
    lock = threading.Lock()
    done_count = [0]

    def _process(row: dict) -> dict:
        fen = row["fen"]
        sf  = row["sf_bestmove"]
        nnue_m, hce_m = _probe_both(
            args.engine, args.nnue, fen, args.movetime, args.threads, args.hash,
            engine2_path=args.engine2,
        )
        return {
            "row":         row,
            "nnue_move":   nnue_m,
            "hce_move":    hce_m,
            "nnue_agrees": nnue_m == sf if nnue_m else False,
            "hce_agrees":  hce_m  == sf if hce_m  else False,
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process, r): r for r in rows}
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                done_count[0] += 1
                results.append(res)
                n = done_count[0]
                nnue_so_far = sum(r["nnue_agrees"] for r in results)
                hce_so_far  = sum(r["hce_agrees"]  for r in results)
                print(f"\r  [{n:3d}/{len(rows)}]  "
                      f"{_label1}: {nnue_so_far}/{n} ({100*nnue_so_far/n:.1f}%)  "
                      f"{_label2}: {hce_so_far}/{n} ({100*hce_so_far/n:.1f}%)",
                      end="", flush=True)

    print()  # newline after progress

    # ---- aggregate --------------------------------------------------------

    # Overall
    nnue_total = sum(r["nnue_agrees"] for r in results)
    hce_total  = sum(r["hce_agrees"]  for r in results)
    n_total    = len(results)

    # By category
    cat_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "nnue": 0, "hce": 0})
    for r in results:
        for cat in _cats(r["row"]):
            cat_stats[cat]["n"]    += 1
            cat_stats[cat]["nnue"] += r["nnue_agrees"]
            cat_stats[cat]["hce"]  += r["hce_agrees"]

    # By depth bucket (how hard the positions are for SF to find)
    depth_stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "nnue": 0, "hce": 0})
    for r in results:
        b = _depth_bucket(r["row"])
        depth_stats[b]["n"]    += 1
        depth_stats[b]["nnue"] += r["nnue_agrees"]
        depth_stats[b]["hce"]  += r["hce_agrees"]

    # ---- print report -----------------------------------------------------

    L1 = _label1; L2 = _label2
    print(f"\n{'─'*72}")
    print(f"  OVERALL")
    print(f"  {L1}: {nnue_total}/{n_total}  ({100*nnue_total/n_total:.1f}%)")
    print(f"  {L2}: {hce_total}/{n_total}  ({100*hce_total/n_total:.1f}%)")
    delta = 100 * (nnue_total - hce_total) / n_total
    winner = L1 if nnue_total > hce_total else (L2 if hce_total > nnue_total else "TIE")
    sign = "+" if delta >= 0 else ""
    print(f"  Δ   : {sign}{delta:.1f}pp  →  {winner} is better overall")

    print(f"\n{'─'*72}")
    print(f"  {'Category':<22} {'N':>4}  {L1:>8}  {L2:>8}  {L1+'-'+L2:>9}  {'Winner':<6}")
    print(f"  {'-'*22}  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*9}  {'-'*6}")
    for cat in sorted(cat_stats, key=lambda c: -cat_stats[c]["n"]):
        s = cat_stats[cat]
        winner_cat = (L1 if s["nnue"] > s["hce"]
                      else (L2 if s["hce"] > s["nnue"] else "tie"))
        print(f"  {cat:<22} {s['n']:>4}  {_pct(s['nnue'],s['n']):>8}  "
              f"{_pct(s['hce'],s['n']):>8}  {_delta(s['nnue'],s['hce'],s['n']):>9}  {winner_cat:<6}")

    print(f"\n{'─'*72}")
    print(f"  {'Depth bucket':>15}  {'N':>4}  {L1:>8}  {L2:>8}  {L1+'-'+L2:>9}")
    print(f"  {'-'*15}  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*9}")
    for bucket in ["d1-10", "d11-15", "d16-20", "d21-25", "d26+", "never"]:
        s = depth_stats.get(bucket, {"n": 0, "nnue": 0, "hce": 0})
        print(f"  {bucket:>15}  {s['n']:>4}  {_pct(s['nnue'],s['n']):>8}  "
              f"{_pct(s['hce'],s['n']):>8}  {_delta(s['nnue'],s['hce'],s['n']):>9}")

    # Positions where they disagree
    nnue_only = [r for r in results if r["nnue_agrees"] and not r["hce_agrees"]]
    hce_only  = [r for r in results if r["hce_agrees"]  and not r["nnue_agrees"]]

    print(f"\n{'─'*72}")
    print(f"  {L1} finds SF move but {L2} doesn't : {len(nnue_only)}")
    print(f"  {L2} finds SF move but {L1} doesn't : {len(hce_only)}")

    if nnue_only:
        print(f"\n  ── {L1} exclusive wins (first 10) ──")
        for r in nnue_only[:10]:
            row = r["row"]
            print(f"    {row['game_id']} ply={row['ply']:>3}  "
                  f"SF={row['sf_bestmove']:<6} {L1}={r['nnue_move']:<6} {L2}={r['hce_move'] or '?':<6}  "
                  f"cats={row.get('categories')}")

    if hce_only:
        print(f"\n  ── {L2} exclusive wins (first 10) ──")
        for r in hce_only[:10]:
            row = r["row"]
            print(f"    {row['game_id']} ply={row['ply']:>3}  "
                  f"SF={row['sf_bestmove']:<6} {L2}={r['hce_move']:<6} {L1}={r['nnue_move'] or '?':<6}  "
                  f"cats={row.get('categories')}")

    print(f"\n{'='*72}")

    # ---- optional JSON output --------------------------------------------

    if args.out:
        out_data = {
            "meta": {
                "engine":   args.engine,
                "nnue":     args.nnue,
                "movetime": args.movetime,
                "n":        n_total,
            },
            "summary": {
                "nnue_agrees": nnue_total,
                "hce_agrees":  hce_total,
                "nnue_pct":    round(100 * nnue_total / n_total, 2) if n_total else 0,
                "hce_pct":     round(100 * hce_total  / n_total, 2) if n_total else 0,
            },
            "by_category": {
                cat: {"n": s["n"], "nnue": s["nnue"], "hce": s["hce"]}
                for cat, s in sorted(cat_stats.items())
            },
            "by_depth":    {
                b: {"n": s["n"], "nnue": s["nnue"], "hce": s["hce"]}
                for b, s in depth_stats.items()
            },
            "positions": [
                {
                    "game_id":    r["row"]["game_id"],
                    "ply":        r["row"]["ply"],
                    "sf_move":    r["row"]["sf_bestmove"],
                    "nnue_move":  r["nnue_move"],
                    "hce_move":   r["hce_move"],
                    "nnue_agrees": r["nnue_agrees"],
                    "hce_agrees":  r["hce_agrees"],
                    "categories": r["row"].get("categories", []),
                }
                for r in sorted(results, key=lambda x: (x["row"]["game_id"], x["row"]["ply"]))
            ],
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(out_data, f, indent=2)
        print(f"\n  Results written to: {args.out}")


if __name__ == "__main__":
    main()
