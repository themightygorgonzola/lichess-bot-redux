#!/usr/bin/env python3
"""
sprt_tune.py -- Run a timed SPRT match between two engine binaries.

Usage
-----
    python tools/sprt_tune.py \\
        --base  bot/engine/redux-hce.exe  \\
        --test  bot/engine/redux-hce-dev.exe \\
        --tc    movetime=100 \\
        --elo0  0  --elo1  3  \\
        --max-games  10000

Time control formats (--tc):
    movetime=<ms>           e.g. movetime=100
    depth=<n>               e.g. depth=8
    <wtime>+<inc>           e.g. 5000+50  (ms)

Exit codes:
    0  H1 accepted  (test engine is better by >= elo1 with probability 1-alpha)
    1  H0 accepted  (test engine is not better)
    2  Max games reached without conclusion (inconclusive)
    3  Error
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

# Ensure tools/ root is on the path for sprt.py and uci/ imports
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/ root
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from sprt import SPRTState

from uci.engine  import UCIEngine
from uci.player  import UCIPlayer, TimeControl
from uci.match   import MatchRunner, MatchConfig
from uci.game    import GameResult


# ---------------------------------------------------------------------------
# Time-control parser
# ---------------------------------------------------------------------------

def _parse_tc(tc_str: str) -> TimeControl:
    """Parse --tc argument into a TimeControl object."""
    if tc_str.startswith("movetime="):
        ms = int(tc_str.split("=", 1)[1])
        return TimeControl(movetime_ms=ms)
    if tc_str.startswith("depth="):
        d = int(tc_str.split("=", 1)[1])
        return TimeControl(depth=d)
    if "+" in tc_str:
        base, inc = tc_str.split("+", 1)
        base_ms, inc_ms = int(base), int(inc)
        return TimeControl(wtime=base_ms, btime=base_ms, winc=inc_ms, binc=inc_ms)
    raise ValueError(
        f"Unrecognised --tc format: {tc_str!r}. "
        "Use 'movetime=<ms>', 'depth=<n>', or '<base>+<inc>'."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SPRT match between two UCI engine binaries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base",      required=True,  help="Baseline engine path")
    parser.add_argument("--test",      required=True,  help="Test engine path")
    parser.add_argument("--tc",        default="movetime=100", help="Time control")
    parser.add_argument("--elo0",      type=float, default=0.0,  help="H0 Elo diff (default 0)")
    parser.add_argument("--elo1",      type=float, default=3.0,  help="H1 Elo diff (default 3)")
    parser.add_argument("--alpha",     type=float, default=0.05, help="False-positive rate")
    parser.add_argument("--beta",      type=float, default=0.05, help="False-negative rate")
    parser.add_argument("--max-games", type=int,   default=10000, dest="max_games",
                        help="Hard cap on games played (default 10000)")
    parser.add_argument("--threads",   type=int,   default=1,    help="Threads per engine")
    parser.add_argument("--hash",      type=int,   default=64,   dest="hash_mb",
                        help="Hash size per engine in MB (default 64)")
    parser.add_argument("--pgn",       default=None, help="Write games to PGN file")
    parser.add_argument("--batch",     type=int,   default=2,
                        help="Games per batch between SPRT checks (must be even, default 2)")
    args = parser.parse_args()

    # Validate
    if not os.path.isfile(args.base):
        print(f"ERROR: base engine not found: {args.base}", file=sys.stderr)
        return 3
    if not os.path.isfile(args.test):
        print(f"ERROR: test engine not found: {args.test}", file=sys.stderr)
        return 3

    batch = max(2, args.batch - (args.batch % 2))  # force even (pairs of games)
    tc    = _parse_tc(args.tc)
    sprt  = SPRTState(elo0=args.elo0, elo1=args.elo1, alpha=args.alpha, beta=args.beta)

    print(f"SPRT test: base={os.path.basename(args.base)}  "
          f"test={os.path.basename(args.test)}")
    print(f"  TC={args.tc}  H0={args.elo0:+.1f}  H1={args.elo1:+.1f}  "
          f"alpha={args.alpha}  beta={args.beta}  max={args.max_games} games")
    print(f"  Boundaries: LLR in [{sprt.lo:.3f}, {sprt.hi:.3f}]")
    print()

    # SPRT callback -- called after each game by MatchRunner
    # game_idx is 0-based; p1=test engine when game_idx is even (color swap)
    conclusion_event = threading.Event()
    conclusion_result: dict = {}

    def on_game(gr: GameResult, game_idx: int) -> None:
        # Determine result from test engine's perspective
        # p1 = test engine when game_idx is even (white), black when odd
        p1_color = "white" if game_idx % 2 == 0 else "black"
        if gr.winner is None:
            sprt.update("draw")
        elif gr.winner == p1_color:
            sprt.update("win")
        else:
            sprt.update("loss")

        print(f"\r  {sprt.summary()}", end="", flush=True)

        verdict = sprt.conclusion()
        if verdict is not None:
            conclusion_result["verdict"] = verdict
            conclusion_event.set()

    # Run in batches, checking for conclusion after each batch
    games_played = 0
    verdict = None

    # Initialise engines once -- reuse across batches
    print("  Starting engines...", end="", flush=True)
    base_engine = UCIEngine(args.base, threads=args.threads, hash_mb=args.hash_mb)
    test_engine = UCIEngine(args.test, threads=args.threads, hash_mb=args.hash_mb)
    print(f" OK  ({base_engine.name} vs {test_engine.name})")

    p1 = UCIPlayer(test_engine, label=f"test({os.path.basename(args.test)})")
    p2 = UCIPlayer(base_engine, label=f"base({os.path.basename(args.base)})")

    try:
        while games_played < args.max_games and not conclusion_event.is_set():
            remaining = args.max_games - games_played
            this_batch = min(batch, remaining - (remaining % 2) or batch)
            if this_batch <= 0:
                break

            cfg = MatchConfig(
                games=this_batch,
                tc=tc,
                swap_colors=True,
                pgn_path=args.pgn,
                verbose=False,
                on_game_end=on_game,
            )
            MatchRunner(p1, p2, cfg).run()
            games_played += this_batch

            verdict = conclusion_result.get("verdict")
            if verdict:
                break

    finally:
        base_engine.quit()
        test_engine.quit()

    print()  # newline after \r progress

    # Final summary
    print()
    print("=" * 60)
    print(f"Final: {sprt.summary()}")
    if verdict == "H1_accepted":
        print(f"RESULT: H1 accepted -- test engine is stronger (>={args.elo1:+.1f} Elo)")
        return 0
    elif verdict == "H0_accepted":
        print(f"RESULT: H0 accepted -- no significant improvement detected")
        return 1
    else:
        print(f"RESULT: Inconclusive after {sprt.games} games")
        return 2


if __name__ == "__main__":
    sys.exit(main())
