#!/usr/bin/env python3
"""
structural_audit.py â€” Run 10 targeted positions through the engine to surface
structural evaluation issues (not constant-tuning gaps).

Each position is chosen to stress a specific known gap in eval.cpp:
  1.  King safety: single-attacker threshold (fires only at attackers_count >= 2)
  2.  Rook mobility: scores pawn-attacked squares (minors avoid them; rooks don't)
  3.  Pawn storm / shield gap pattern â€” shield counts pawns but not gapped vs solid
  4.  Passed pawn blockade â€” no bonus for own piece sitting on stop square
  5.  King-passer distance: Manhattan vs Chebyshev (king steps != |dx|+|dy|)
  6.  Bishop of wrong color: does eg score reflect draw-ish nature correctly?
  7.  Rook dominance on 7th: king on 8th + enemy pawns on 7th â€” correct bonus?
  8.  Knight outpost vs mobile bishop in open position
  9.  Trapped piece: bishop hemmed in by own pawn structure
  10. Open-file dynamics: opposing rooks on half-open files

Usage:
    python tools/structural_audit.py [--depth N] [--movetime MS] [--engine PATH]
"""

from __future__ import annotations
import argparse
import os
import subprocess
import sys
import threading
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
from engine_config import find_engine

# ---------------------------------------------------------------------------
# Positions
# Each entry: (tag, fen, probe_note)
# ---------------------------------------------------------------------------
POSITIONS = [
    # â”€â”€ 1. King safety: single queen attacker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Black queen on d4 attacks white king on e1. Only 1 attacker so
    # attackers_count < 2 â†’ safety_penalty = 0 even though the queen
    # is right next door.  Expect: white should be evaluated as worse.
    (
        "P1 :: king-safety single-attacker",
        "4k2r/ppp2ppp/8/8/3qP3/8/PPP2PPP/4K2R w Kk - 0 1",
        "White king on e1, black Qd4 attacking king zone. Only 1 attacker â†’ "
        "safety threshold NOT triggered. Engine should still see danger "
        "(queen near king + enemy rook on h8), compare to P1b below.",
    ),
    (
        "P1b :: king-safety two attackers (control)",
        "4k2r/ppp2ppp/8/8/3qP3/7n/PPP2PPP/4K2R w Kk - 0 1",
        "Same as P1 but add Nh3 giving 2 attackers. Safety table should now "
        "fire. The delta between P1 and P1b eval reveals how much the "
        "single-attacker gap costs.",
    ),
    # â”€â”€ 2. Rook mobility on pawn-attacked squares â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Black has pawns on c5/d5 attacking d4/c4/e4. White rook on d1 would get
    # full mobility credit even to d4 (attacked by c5 pawn).
    # Expectation: white's rook should not score as highly as naive mobility count.
    (
        "P2 :: rook mobility pawn-attacked squares",
        "r3k3/ppp2ppp/8/2ppp3/3R4/8/PPP2PPP/4K3 w - - 0 1",
        "White Rd4 has wide mobility but many squares are pawn-attacked. "
        "Knights/bishops exclude these; rooks include them. Compare "
        "effective piece activity vs raw mobility score.",
    ),
    # â”€â”€ 3. Pawn shield gap vs solid shield â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # P3a: castled king with f-pawn advanced â€” gap in shield (g2,h2 only)
    # P3b: solid castled king with g2,h2,f2 intact (control)
    # The engine scores pawn count only; a gap on the f-file matters more than
    # losing a pawn elsewhere.
    (
        "P3a :: pawn shield gap (f-pawn advanced)",
        "5rk1/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/5RK1 b - - 0 1",
        "White king on g1, f-pawn gone. Two shield pawns (g2, h2). "
        "Black should be slightly better due to the g1-a7 diagonal opening "
        "and weakened f1-h3 diagonal.",
    ),
    (
        "P3b :: solid pawn shield (control)",
        "5rk1/ppppbppp/8/4p3/4P3/5N2/PPPPBPPP/5RK1 b - - 0 1",
        "White king solid on g1 with f2/g2/h2 all present. "
        "Compare eval to P3a to see how much the broken shield is penalized.",
    ),
    # â”€â”€ 4. Passed pawn blockade â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # White has a passer on d5, black knight is directly on d6 (blockade).
    # No blockade bonus in eval â€” engine might over-value the passer.
    (
        "P4 :: passed pawn blocked by knight",
        "8/p4k2/3n4/3P4/8/8/P4K2/8 w - - 0 1",
        "White d5 passer blocked by Nd6. Eval gives full passer bonus but "
        "passer can't advance without trading. Black should hold more easily "
        "than raw passed-pawn bonus suggests.",
    ),
    # â”€â”€ 5. King-passer distance: Chebyshev vs Manhattan â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # P5: king diagonally adjacent to pawn â€” 1 Chebyshev step but 2 Manhattan.
    # eval uses |dx|+|dy| (Manhattan), so king on e3 supporting d4 passer scores
    # worse than it should (2 instead of 1).
    (
        "P5 :: king diagonal support of passer (Chebyshev gap)",
        "8/6k1/8/8/3P4/4K3/8/8 w - - 0 1",
        "White Ke3 diagonally supports d4 passer â€” 1 king step away (Chebyshev) "
        "but Manhattan = 2. Engine uses Manhattan so understates king support. "
        "Expected: white should win; check eval magnitude vs P5b.",
    ),
    (
        "P5b :: king directly in front (Manhattan == Chebyshev, control)",
        "8/6k1/8/8/3P4/3K4/8/8 w - - 0 1",
        "White Kd3 directly supports d4 passer â€” Chebyshev = Manhattan = 1. "
        "Control for P5. Delta between P5 and P5b reveals Manhattan bias.",
    ),
    # â”€â”€ 6. Bad bishop in closed center â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Fixed pawn chain on dark squares; white bishop on d3 is hemmed in.
    # bad_bishop_per_pawn term penalizes by own pawns on same color,
    # but does mobility term correctly reinforce this?
    (
        "P6 :: bad bishop + good knight in closed position",
        "r1bq1rk1/pp2ppbp/2pp1np1/8/2PPP3/2N2N2/PP2BPPP/R1BQ1RK1 b - - 0 9",
        "Closed center with pawn chain on light squares. White Bc1/Be2 both "
        "on light squares but pawns block them. Black Nf6 in open position. "
        "Does mobility correctly depress bishop score relative to knight?",
    ),
    # â”€â”€ 7. Rook on 7th rank bonus â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Rook on the 7th is evaluated correctly only when enemy king is on 8th
    # OR enemy pawns are on 7th. Test: rook on 7th, king on 6th (no trigger).
    (
        "P7 :: rook on 7th, king NOT on 8th (bonus should not fire)",
        "8/R7/5k2/8/8/5K2/8/8 w - - 0 1",
        "White Ra7 on 7th rank. Black king on f6, NOT on 8th. "
        "rook_seventh bonus should be 0 here. Check if king proximity "
        "matters more than the 7th rank rule.",
    ),
    (
        "P7b :: rook on 7th, king on 8th (bonus should fire)",
        "5k2/R7/8/8/8/5K2/8/8 w - - 0 1",
        "White Ra7 on 7th, black king on f8 (8th rank). "
        "Bonus should fire here. Delta P7 vs P7b = rook_seventh value.",
    ),
    # â”€â”€ 8. Knight outpost vs roaming bishop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Classic textbook: knight firmly planted on d5 outpost, bishop has scope
    # but no plan. Engine should strongly prefer the knight side.
    (
        "P8 :: knight outpost d5 dominance",
        "r2r2k1/pp3ppp/4b3/2pN4/8/2P3P1/PP3PKP/2RR4 w - - 0 22",
        "White Nd5 on ideal outpost supported by c4 pawn, no enemy pawn "
        "can challenge it. Black bishop passive. Expect strong white advantage. "
        "Checks outpost bonus magnitude and mobility delta.",
    ),
    # â”€â”€ 9. Connected vs disconnected passed pawns â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Two connected passers on d5/e5 should score far better than two isolated
    # passers. Tests candidate_passer / protected_passer interaction.
    (
        "P9 :: connected passed pawns",
        "8/6k1/8/3PP3/8/8/8/3K4 w - - 0 1",
        "White d5+e5 connected passers. Both are passed and connected. "
        "Should score significantly higher than two isolated passers. "
        "Check that protected_passer_mg/eg fires on both.",
    ),
    # â”€â”€ 10. Open file battery: doubled rooks vs lone rook â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # White has doubled rooks on the d-file (open). Black has a single rook.
    # connected_rooks_bonus + 2Ã— rook_open_file should reflect the dominance.
    (
        "P10 :: doubled rooks on open file",
        "8/pp4kp/8/8/3RR3/8/PP4KP/8 w - - 0 1",
        "White Rd4+Re4 doubled on open files. Black no rooks. "
        "Expect strong white advantage. Tests connected_rooks + open file "
        "bonuses stacking correctly.",
    ),
]

# ---------------------------------------------------------------------------

def probe(engine: str, fen: str, depth: int | None, movetime: int, threads: int) -> list[str]:
    env = os.environ.copy()
    mingw = r"C:\mingw64\bin"
    if os.path.isdir(mingw) and mingw not in env.get("PATH", ""):
        env["PATH"] = mingw + os.pathsep + env.get("PATH", "")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    parts = fen.strip().split()
    if len(parts) == 4:
        fen = fen.strip() + " 0 1"

    go_cmd = f"go depth {depth}" if depth else f"go movetime {movetime}"
    cmds = [
        "uci\n",
        f"setoption name Threads value {threads}\n",
        "setoption name Hash value 128\n",
        "isready\n",
        f"position fen {fen}\n",
        f"{go_cmd}\n",
    ]

    proc = subprocess.Popen(
        engine,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env, creationflags=flags,
    )
    for cmd in cmds:
        proc.stdin.write(cmd)
        proc.stdin.flush()

    lines: list[str] = []
    timeout = (depth * 8) if depth else (movetime / 1000 + 5)

    def reader():
        for ln in proc.stdout:
            ln = ln.rstrip()
            lines.append(ln)
            if ln.startswith("bestmove"):
                try:
                    proc.stdin.write("quit\n")
                    proc.stdin.flush()
                except OSError:
                    pass
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout + 3)
    try:
        proc.kill()
    except OSError:
        pass
    return lines


def parse_result(lines: list[str]) -> tuple[str, str, str]:
    """Return (best_score, best_move, best_pv)."""
    best_score = "?"
    best_move = "?"
    best_pv = ""
    for ln in lines:
        if ln.startswith("info") and "depth" in ln and "score" in ln:
            toks = ln.split()
            score_str = pv_str = None
            i = 0
            while i < len(toks):
                if toks[i] == "score" and i + 2 < len(toks):
                    kind = toks[i + 1]
                    val = toks[i + 2]
                    if kind in ("cp", "mate"):
                        score_str = f"{kind} {val}"
                        best_score = score_str
                elif toks[i] == "pv":
                    pv_str = " ".join(toks[i + 1: i + 7])
                i += 1
            if score_str and pv_str:
                best_pv = pv_str
        elif ln.startswith("bestmove"):
            toks = ln.split()
            best_move = toks[1] if len(toks) > 1 else "?"
    return best_score, best_move, best_pv


def main() -> None:
    ap = argparse.ArgumentParser(description="Structural eval audit â€” 10 targeted positions")
    ap.add_argument("--depth",    type=int, default=1,
                    help="Search depth (default 1 = static+qsearch; use 12+ for full search)")
    ap.add_argument("--movetime", type=int, default=0,   help="Movetime ms if --depth not set")
    ap.add_argument("--threads",  type=int, default=4,   help="UCI Threads (default 4)")
    ap.add_argument("--engine",   default=None,          help="Engine binary path")
    args = ap.parse_args()

    engine = args.engine or find_engine()
    if not engine:
        sys.exit("[audit] Engine not found. Build with: .\\make.ps1 build")

    use_depth = args.depth if args.depth else None
    use_movetime = args.movetime if not use_depth else 0

    print(f"\n{'='*76}")
    print(f"  Structural Eval Audit")
    print(f"  Engine : {engine}")
    print(f"  Mode   : {'depth ' + str(use_depth) if use_depth else str(use_movetime) + ' ms'}")
    print(f"  Threads: {args.threads}")
    print(f"{'='*76}\n")

    results = []
    for tag, fen, note in POSITIONS:
        print(f"  Probing {tag} ...")
        sys.stdout.flush()
        lines = probe(engine, fen, use_depth, use_movetime, args.threads)
        score, move, pv = parse_result(lines)
        results.append((tag, fen, note, score, move, pv))

    # â”€â”€ Report â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(f"\n{'='*76}")
    print(f"  RESULTS")
    print(f"{'='*76}")

    for tag, fen, note, score, move, pv in results:
        print(f"\n{'â”€'*76}")
        print(f"  {tag}")
        print(f"  FEN   : {fen}")
        print(f"  Score : {score:<18}  bestmove = {move}")
        print(f"  PV    : {pv}")
        print(f"  âš‘  {note}")

    # â”€â”€ Pair comparisons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print(f"\n{'='*76}")
    print(f"  PAIR COMPARISONS (eval gap analysis)")
    print(f"{'='*76}")

    pairs = [
        ("P1 vs P1b",  "King safety single- vs two-attacker",   0, 1),
        ("P3a vs P3b", "Shield gap vs solid shield",             3, 4),
        ("P5 vs P5b",  "King diagonal vs direct support",        6, 7),
        ("P7 vs P7b",  "Rook 7th: king not on 8th vs on 8th",   9, 10),
    ]

    def parse_cp(score_str: str) -> int | None:
        parts = score_str.split()
        if len(parts) == 2 and parts[0] == "cp":
            try:
                return int(parts[1])
            except ValueError:
                pass
        return None

    for label, desc, ia, ib in pairs:
        sa = results[ia][3]
        sb = results[ib][3]
        cpa = parse_cp(sa)
        cpb = parse_cp(sb)
        if cpa is not None and cpb is not None:
            delta = cpb - cpa
            print(f"\n  {label} â€” {desc}")
            print(f"    A ({results[ia][0]}): {sa}")
            print(f"    B ({results[ib][0]}): {sb}")
            print(f"    Î” (B-A) = {delta:+d} cp  â† "
                  f"{'expected significant gap' if abs(delta) < 30 else 'reasonable gap'}")
        else:
            print(f"\n  {label} â€” {desc}")
            print(f"    A: {sa}   B: {sb}  (non-cp, cannot diff)")

    print(f"\n{'='*76}")
    print(f"  STRUCTURAL NOTES")
    print(f"{'='*76}")
    print("""
  Known eval gaps probed above:

  [P1/P1b] attackers_count >= 2 threshold
           If Î”(P1b-P1a) is very large, the single-attacker scenario is
           genuinely under-penalized. Fix: add partial safety for 1 attacker
           (e.g. queen alone = 50% table weight).

  [P2]     Rook mobility on pawn-attacked squares
           Compare white eval here vs a symmetrical position with pawns absent.
           Fix: apply safe-square filter to rook mobility too.

  [P3a/b]  Pawn shield gaps (file pattern)
           Eval only counts shield pawns. A gap on f-file is more dangerous
           than losing an h-file pawn. Fix: per-file penalty for missing shield.

  [P4]     Passed pawn blockade
           Passer bonus fires regardless of whether the stop square is occupied.
           Fix: discount passer bonus when stop square holds a blocking piece.

  [P5/P5b] King-passer: Manhattan vs Chebyshev distance
           King diagonally adjacent uses Manhattan=2 but is 1 king step away.
           Fix: replace std::abs(dx)+std::abs(dy) with std::max(|dx|,|dy|).

  [P6]     Bad bishop + mobility cross-check
           bad_bishop_per_pawn should correlate with depressed mobility score.
           If mobility is still high despite bad bishop, the terms fight each other.

  [P7/P7b] Rook on 7th rank trigger condition
           Condition: king == RANK_8 OR enemy pawns on 7th.
           Only king-on-8th is reliable; pawn-on-7th can trigger spuriously.

  [P8]     Knight outpost vs roaming bishop
           Outpost bonus + negative bishop mobility should together dominate.

  [P9]     Connected passers
           Both pawns should trigger protected_passer; check both fire.

  [P10]    Doubled rooks: connected_rooks + open_file stacking
           Verify the bonuses accumulate correctly for 2 rooks on 2 open files.
""")


if __name__ == "__main__":
    main()
