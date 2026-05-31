#!/usr/bin/env python3
"""
structural_audit.py — Stress-test specific structural evaluation gaps vs Stockfish.

10 carefully chosen positions that target known weaknesses in our eval:

  1.  King safety: uncastled king with open lines toward it
  2.  Passed pawn blockade: blocked passer — piece on stop square
  3.  Bad bishop: bishop locked behind its own pawns
  4.  Knight outpost: knight strongly entrenched on deep outpost vs loose bishop
  5.  Rook on 7th rank: dominant rook vs passive rook
  6.  IQP (isolated queen pawn): IQP compensation + dynamic
  7.  King tropism in endgame: king centralization in pawn endgame
  8.  Weak color complex: one side owns all squares of the missing bishop's color
  9.  Outside passed pawn: should dominate opposing K+P endgame
  10. Pawn race (king distance): requires Chebyshev, not Manhattan, distance

For each position:
  - Ground truth: Stockfish depth 20 eval (centipawns, side-to-move perspective)
  - Our eval: depth 1 (static) and depth 10 (search)
  - Flag: whether our sign matches SF, and magnitude of error

Usage
-----
    python tools/analysis/structural_audit.py
    python tools/analysis/structural_audit.py --depth 10
    python tools/analysis/structural_audit.py \\
        --engine build/redux-hce.exe \\
        --sf engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe
"""
from __future__ import annotations

import argparse
import json
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
def cyan(t):   return _esc("96", t)

# ── Default paths ─────────────────────────────────────────────────────────────
_SF_CANDIDATES  = [
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe",
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64.exe",
]
_ENG_CANDIDATES = ["build/redux-hce.exe", "bot/engine/redux-hce.exe"]


def _find(candidates: list[str], label: str) -> str:
    for rel in candidates:
        p = _ROOT / rel
        if p.exists():
            return str(p)
    raise FileNotFoundError(f"{label} not found among: {candidates}")


# ── Audit positions ───────────────────────────────────────────────────────────
# Each entry: (id, name, theme, fen, expected_sign, notes)
#   expected_sign: +1 if white should be better per SF, -1 if black, 0 if ~equal
#   The FEN side-to-move is white unless noted in the name.
#
# All positions are chosen so the structural factor is clear and unambiguous.
AUDIT_POSITIONS = [
    # ── 1. King safety ──────────────────────────────────────────────────────
    # White king stuck in center, open e-file, black has rooks developed.
    # Theme: eval should penalize uncastled king under pressure significantly.
    (1, "king_safety_open_center",
     "King safety: uncastled king, open e-file, black active",
     "r1bqk2r/ppp2ppp/2np1n2/4p3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R w KQkq - 0 6",
     -1,
     "White's king has NOT castled, black is better developed. "
     "SF should penalize white. Our eval must capture the king safety deficit."),

    # ── 2. Passed pawn blockade ──────────────────────────────────────────────
    # White has a passed d5 pawn but black knight sits on d6 (the stop square).
    # Theme: eval should discount the passed pawn's value when piece blocks stop sq.
    (2, "passed_pawn_blockade",
     "Passed pawn blockade: knight on stop square",
     "r4rk1/pp3ppp/2nb4/3pN3/8/2NP4/PP3PPP/R4RK1 w - - 0 14",
     -1,
     "White's knight is on e5, but black knight blockades d6 (stop sq for d5). "
     "SF should consider this roughly equal or slightly black. "
     "Weak eval may over-value the white passer."),

    # ── 3. Bad bishop ────────────────────────────────────────────────────────
    # White bishop is locked behind its own pawns on d4/e5. Pawn structure is locked.
    # Theme: bad bishop penalty — bishop is passive, knight is better.
    (3, "bad_bishop_locked",
     "Bad bishop: white's bishop locked behind d4/e5 pawns",
     "r2q1rk1/pp3ppp/2n1bn2/4p3/3PP3/2PB1N2/PP3PPP/R1BQ1RK1 w - - 0 10",
     -1,
     "White's dark-squared bishop is locked behind d4/e5. "
     "SF should prefer black (or equal). Our eval may over-rate the bishop."),

    # ── 4. Knight outpost ─────────────────────────────────────────────────────
    # White knight on d5: deep outpost, no pawn kicks it, black has opposite bishop.
    # Theme: knight outpost bonus — should be clearly significant.
    (4, "knight_outpost_d5",
     "Knight outpost: white Nd5 vs black's bishop pair",
     "r2q1rk1/pp2ppbp/2p3p1/3Nn3/3P4/2P1B3/PP2BPPP/R2Q1RK1 w - - 0 12",
     +1,
     "White knight dominates d5, cannot be kicked. "
     "SF should give white a clear edge. Weak outpost eval will underestimate."),

    # ── 5. Rook on 7th ───────────────────────────────────────────────────────
    # White rook on 7th rank, active. Black rook is passive.
    # Theme: rook-on-7th bonus must be significant.
    (5, "rook_on_seventh",
     "Rook on 7th: white Ra7 vs black Rb8",
     "1r4k1/R5pp/4p3/8/8/8/6PP/6K1 w - - 0 1",
     +1,
     "White rook dominates the 7th rank. "
     "SF should give white the advantage. Our eval must reward this."),

    # ── 6. IQP dynamics ──────────────────────────────────────────────────────
    # White has an isolated d-pawn but all minor pieces are active.
    # Theme: IQP compensation — activity and space should balance the weakness.
    (6, "iqp_compensation",
     "IQP: white isolated d4, active pieces vs solid black setup",
     "r1bq1rk1/pp3ppp/2n1bn2/3pp3/3P4/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 10",
     0,
     "IQP middlegame — should be roughly equal, maybe slightly black. "
     "Our eval may mis-weight the IQP weakness vs activity."),

    # ── 7. King centralization endgame ──────────────────────────────────────
    # Pure king and pawn endgame — white king is more centralized.
    # Theme: king activity in endgame; should show measurable edge.
    (7, "king_activity_endgame",
     "King endgame: centralized vs passive king",
     "8/pp6/8/3k4/3K4/8/PP6/8 w - - 0 1",
     +1,
     "White king is active on d4 (same rank as black d5). "
     "With initiative, white should have a small edge. "
     "Eval must detect king activity, not just material."),

    # ── 8. Weak color complex ────────────────────────────────────────────────
    # Black has traded off its dark-squared bishop. All dark squares are weak.
    # Theme: weak color complex penalty should be large.
    (8, "weak_color_complex",
     "Weak color complex: black's dark squares gaping after bishop trade",
     "r2q1rk1/pp3ppp/2n1pn2/1Bpp4/3P4/2P1PN2/PP3PPP/R1BQ1RK1 w - - 0 10",
     +1,
     "Black traded its dark-squared bishop; white's Bb5 exploits weak dark squares. "
     "SF should give white a clear edge. Weak color-complex eval will miss this."),

    # ── 9. Outside passed pawn ───────────────────────────────────────────────
    # White has an outside passed a-pawn in a K+P endgame, forcing black K away.
    # Theme: outside passer is decisive; king must cover both sides.
    (9, "outside_passed_pawn",
     "Outside passed pawn: white a-pawn vs black K+pawns on kingside",
     "8/5k2/4p1p1/4P1P1/P7/8/5K2/8 w - - 0 1",
     +1,
     "White's a-pawn is an outside passer — forces black king to abandon kingside. "
     "SF evaluates this as winning for white. Our eval must see this pattern."),

    # ── 10. Pawn race — Chebyshev distance ──────────────────────────────────
    # Both sides have a passer racing to promote. Requires Chebyshev (max) king dist.
    # Theme: Manhattan distance gives wrong answer here — Chebyshev is correct.
    (10, "pawn_race_chebyshev",
     "Pawn race: result depends on Chebyshev (max) vs Manhattan king distance",
     "8/8/3k4/1P6/8/8/6p1/3K4 w - - 0 1",
     -1,
     "White b5 pawn races black g2 pawn. With correct Chebyshev king distance, "
     "black wins (or draws). Manhattan distance gives an incorrect result. "
     "SF says black wins. Our eval using wrong distance metric will be wrong."),
]


# ── Engine runner ─────────────────────────────────────────────────────────────
def _get_eval(engine_path: str, fen: str, depth: int,
              extra_options: dict | None = None) -> Optional[int]:
    """Run engine to depth, return final score in centipawns from STM perspective."""
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
    score: Optional[int] = None
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
                        score = int(parts[i + 2])
                    except ValueError:
                        pass
                    break
                elif parts[i + 1] == "mate":
                    try:
                        m = int(parts[i + 2])
                        score = 10000 if m > 0 else -10000
                    except ValueError:
                        pass
                    break
            i += 1
        if score is not None:
            break

    return score


# ── Audit runner ──────────────────────────────────────────────────────────────
def run_audit(sf_path: str, eng_path: str,
              sf_depth: int, our_depth: int) -> list[dict]:
    results = []
    for pos_id, name, theme, fen, expected_sign, notes in AUDIT_POSITIONS:
        print(f"  [{pos_id:2d}/10] {name} ...", end=" ", flush=True)

        # Run probes sequentially to avoid multiple heavy engine processes in parallel
        evals: dict[str, Optional[int]] = {}
        evals["sf"]       = _get_eval(sf_path,  fen, sf_depth,   None)
        evals["ours_d1"]  = _get_eval(eng_path, fen, 1,          {"UseNNUE": "false"})
        evals["ours_dn"]  = _get_eval(eng_path, fen, our_depth,  {"UseNNUE": "false"})

        sf_cp   = evals.get("sf")
        d1_cp   = evals.get("ours_d1")
        dn_cp   = evals.get("ours_dn")

        # Determine pass/fail
        sf_sign  = (1 if (sf_cp or 0) > 20 else (-1 if (sf_cp or 0) < -20 else 0))
        d1_sign  = (1 if (d1_cp or 0) > 20 else (-1 if (d1_cp or 0) < -20 else 0))
        dn_sign  = (1 if (dn_cp or 0) > 20 else (-1 if (dn_cp or 0) < -20 else 0))

        d1_err = abs(d1_cp - sf_cp) if d1_cp is not None and sf_cp is not None else None
        dn_err = abs(dn_cp - sf_cp) if dn_cp is not None and sf_cp is not None else None

        d1_sign_ok = (d1_sign == sf_sign) if sf_sign != 0 else None
        dn_sign_ok = (dn_sign == sf_sign) if sf_sign != 0 else None

        # Severity: how much does our d1 eval differ from SF?
        severity = "ok"
        if d1_err is not None:
            if d1_err > 300:
                severity = "critical"
            elif d1_err > 150:
                severity = "major"
            elif d1_err > 75:
                severity = "minor"

        print(f"SF={sf_cp}  d1={d1_cp}(err={d1_err})  d{our_depth}={dn_cp}(err={dn_err})  [{severity}]")

        results.append({
            "id":          pos_id,
            "name":        name,
            "theme":       theme,
            "fen":         fen,
            "expected_sign": expected_sign,
            "notes":       notes,
            "sf_cp":       sf_cp,
            "d1_cp":       d1_cp,
            "dn_cp":       dn_cp,
            "d1_err":      d1_err,
            "dn_err":      dn_err,
            "d1_sign_ok":  d1_sign_ok,
            "dn_sign_ok":  dn_sign_ok,
            "severity":    severity,
        })

    return results


# ── Report ────────────────────────────────────────────────────────────────────
def _print_report(results: list[dict], our_depth: int, sf_depth: int) -> None:
    W = 78
    print("\n" + "=" * W)
    print(bold("  STRUCTURAL AUDIT RESULTS"))
    print("=" * W)
    print(f"  Ground truth: Stockfish depth {sf_depth}")
    print(f"  Our engine:   depth 1 (static) and depth {our_depth}")
    print()

    critical = [r for r in results if r["severity"] == "critical"]
    major    = [r for r in results if r["severity"] == "major"]
    minor    = [r for r in results if r["severity"] == "minor"]
    ok       = [r for r in results if r["severity"] == "ok"]

    total  = len(results)
    d1_pass = sum(1 for r in results if r.get("d1_sign_ok") is True)
    dn_pass = sum(1 for r in results if r.get("dn_sign_ok") is True)
    valid   = sum(1 for r in results if r.get("d1_sign_ok") is not None)

    # Summary header
    print(f"  {'#':>2}  {'Name':<30}  {'SF':>6}  "
          f"{'d1':>7}  {'err':>5}  {'dn':>7}  {'err':>5}  "
          f"{'d1✓':>4}  {'dn✓':>4}  {'severity'}")
    print(f"  {'─'*2}  {'─'*30}  {'─'*6}  "
          f"{'─'*7}  {'─'*5}  {'─'*7}  {'─'*5}  "
          f"{'─'*4}  {'─'*4}  {'─'*8}")

    for r in results:
        sf_s  = f"{r['sf_cp']:+5d}" if r.get("sf_cp") is not None else "   —  "
        d1_s  = f"{r['d1_cp']:+5d}" if r.get("d1_cp") is not None else "   —  "
        dn_s  = f"{r['dn_cp']:+5d}" if r.get("dn_cp") is not None else "   —  "
        e1_s  = f"{r['d1_err']:4d}" if r.get("d1_err") is not None else "  — "
        en_s  = f"{r['dn_err']:4d}" if r.get("dn_err") is not None else "  — "

        d1_ok = r.get("d1_sign_ok")
        dn_ok = r.get("dn_sign_ok")
        d1_sym = (green("  ✓") if d1_ok else (red("  ✗") if d1_ok is False else dim("  ?")))
        dn_sym = (green("  ✓") if dn_ok else (red("  ✗") if dn_ok is False else dim("  ?")))

        sev = r["severity"]
        sev_s = (red("CRITICAL") if sev == "critical" else
                 (yellow("major   ") if sev == "major" else
                  (dim("minor   ") if sev == "minor" else green("ok      "))))

        print(f"  {r['id']:>2}  {r['name']:<30}  {sf_s}  "
              f"{d1_s}  {e1_s}  {dn_s}  {en_s}  "
              f"{d1_sym}   {dn_sym}   {sev_s}")

    print()
    print(f"  SIGN ACCURACY: d1={d1_pass}/{valid}  d{our_depth}={dn_pass}/{valid}  "
          f"(out of {valid} non-equal expected-sign positions)")
    print()

    # Severity breakdown
    print(f"  SEVERITY BREAKDOWN")
    print(f"  {red('CRITICAL')} (err>300cp): {len(critical)}")
    for r in critical:
        print(f"    • {r['name']}: d1={r['d1_cp']} vs SF={r['sf_cp']} (Δ{r['d1_err']}cp)")
    if major:
        print(f"  {yellow('MAJOR')} (150-300cp):   {len(major)}")
        for r in major:
            print(f"    • {r['name']}: d1={r['d1_cp']} vs SF={r['sf_cp']} (Δ{r['d1_err']}cp)")
    if minor:
        print(f"  {dim('MINOR')} (75-150cp):    {len(minor)}")
        for r in minor:
            print(f"    • {r['name']}: d1={r['d1_cp']} vs SF={r['sf_cp']} (Δ{r['d1_err']}cp)")
    if ok:
        print(f"  {green('OK')} (<75cp):          {len(ok)}")
        for r in ok:
            print(f"    • {r['name']}: d1={r['d1_cp']} vs SF={r['sf_cp']} (Δ{r['d1_err']}cp)")

    print()
    print(f"  NOTES ON FAILING POSITIONS")
    print(f"  {'─'*74}")
    for r in sorted(results, key=lambda x: x.get("d1_err") or 0, reverse=True):
        if (r.get("d1_err") or 0) > 75:
            print(f"  [{r['id']}] {r['theme']}")
            print(f"      {dim(r['notes'])}")
            print()

    print("=" * W)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf",       default=None, help="Path to Stockfish binary")
    ap.add_argument("--engine",   default=None, help="Path to our HCE engine binary")
    ap.add_argument("--sf-depth", type=int, default=20,
                    help="Stockfish reference depth (default: 20)")
    ap.add_argument("--depth",    type=int, default=10,
                    help="Our engine search depth (default: 10)")
    ap.add_argument("--json",     default=None, metavar="OUT")
    args = ap.parse_args()

    sf_path  = args.sf     or _find(_SF_CANDIDATES,  "Stockfish")
    eng_path = args.engine or _find(_ENG_CANDIDATES, "redux-hce")

    print(f"\n{'='*78}")
    print(bold("  STRUCTURAL AUDIT"))
    print(f"{'='*78}")
    print(f"  SF (d{args.sf_depth})    : {sf_path}")
    print(f"  Engine (d1/d{args.depth}) : {eng_path}")
    print(f"  Positions      : {len(AUDIT_POSITIONS)}")
    print(f"{'='*78}\n")

    results = run_audit(sf_path, eng_path, args.sf_depth, args.depth)
    _print_report(results, args.depth, args.sf_depth)

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({"sf_depth": args.sf_depth, "our_depth": args.depth,
                       "results": results}, f, indent=2)
        print(f"  Results written to: {out_path}")


if __name__ == "__main__":
    main()
