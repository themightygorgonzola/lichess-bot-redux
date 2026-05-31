#!/usr/bin/env python3
"""
self_play.py â€” Run a self-play match between one or two engine binaries.

Usage examples:

  # 10 games, engine vs itself, 500ms per move
  python tools/self_play.py --games 10 --movetime 500

  # Different thread counts per side, save PGN
  python tools/self_play.py --games 20 --movetime 300 \\
      --p1-threads 8 --p2-threads 1 --pgn match.pgn

  # Fixed depth instead of time
  python tools/self_play.py --games 4 --depth 12

  # Clock-based (e.g. 5 min + 3 sec increment)
  python tools/self_play.py --games 10 --wtime 300000 --btime 300000 --winc 3000 --binc 3000

  # Two different engine binaries
  python tools/self_play.py --engine1 build/lichess-bot.exe \\
      --engine2 /path/to/stockfish.exe --games 10 --movetime 100

  # Single game, show board after each move
  python tools/self_play.py --games 1 --movetime 500 --board
"""

from __future__ import annotations

import argparse
import os
import sys

# Make sure our tools/ package is importable regardless of cwd
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from uci import UCIEngine, UCIPlayer, TimeControl, MatchRunner, MatchConfig, MoveEvent


# ---------------------------------------------------------------------------
# Resolve the default engine path relative to this script
# ---------------------------------------------------------------------------

from engine_config import find_engine as _find_engine_cfg

def _default_engine() -> str:
    try:
        return _find_engine_cfg()
    except FileNotFoundError:
        # Fallback: let the user specify explicitly
        project_ROOT      = os.path.dirname(os.path.dirname(_TOOLS_DIR))
        return os.path.join(project_root, "bot", "engine", "lichess-bot.exe")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run a self-play match between two UCI engine instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Engines
    default_engine = _default_engine()
    p.add_argument("--engine1", metavar="PATH", default=default_engine,
                   help=f"Path to engine 1 binary (default: {default_engine})")
    p.add_argument("--engine2", metavar="PATH", default=None,
                   help="Path to engine 2 binary (default: same as engine1)")
    p.add_argument("--p1-label", metavar="NAME",  default=None,
                   help="Display label for player 1 (default: binary name + thread count)")
    p.add_argument("--p2-label", metavar="NAME",  default=None,
                   help="Display label for player 2")
    p.add_argument("--p1-threads", type=int, default=4,
                   help="Threads for player 1 (default: 4)")
    p.add_argument("--p2-threads", type=int, default=4,
                   help="Threads for player 2 (default: 4)")
    p.add_argument("--p1-hash", type=int, default=128,
                   help="Hash table MB for player 1 (default: 128)")
    p.add_argument("--p2-hash", type=int, default=128,
                   help="Hash table MB for player 2 (default: 128)")

    # Time control (mutually exclusive groups)
    tc = p.add_argument_group("time control (pick one)")
    tc_mx = tc.add_mutually_exclusive_group()
    tc_mx.add_argument("--movetime", type=int, metavar="MS",
                       help="Fixed milliseconds per move")
    tc_mx.add_argument("--depth", type=int, metavar="N",
                       help="Fixed search depth per move")
    tc.add_argument("--wtime", type=int, metavar="MS", default=0,
                    help="White clock (ms); use with --btime for clock-based TC")
    tc.add_argument("--btime", type=int, metavar="MS", default=0,
                    help="Black clock (ms)")
    tc.add_argument("--winc",  type=int, metavar="MS", default=0,
                    help="White increment per move (ms)")
    tc.add_argument("--binc",  type=int, metavar="MS", default=0,
                    help="Black increment per move (ms)")
    tc.add_argument("--movestogo", type=int, metavar="N", default=0,
                    help="Moves until next time control (0 = sudden death)")

    # Match options
    p.add_argument("--games",       type=int, default=2,
                   help="Number of games to play (default: 2)")
    p.add_argument("--fen",         metavar="FEN/startpos", default="startpos",
                   help="Starting position FEN (default: startpos)")
    p.add_argument("--pgn",         metavar="FILE", default=None,
                   help="Write all games to this PGN file")
    p.add_argument("--no-annotate", action="store_true",
                   help="Suppress score annotations in PGN and output")
    p.add_argument("--no-swap",     action="store_true",
                   help="Keep engine1 as white in all games (no color alternation)")
    p.add_argument("--board",       action="store_true",
                   help="Print ASCII board after each move")
    p.add_argument("--quiet",       action="store_true",
                   help="Suppress per-move output (only final summary)")
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    # Resolve engine2 = engine1 if not specified
    engine2_path = args.engine2 or args.engine1

    for path in {args.engine1, engine2_path}:
        if not os.path.isfile(path):
            parser.error(f"Engine binary not found: {path}")

    # Build time control
    if args.movetime:
        tc = TimeControl(movetime_ms=args.movetime)
    elif args.depth:
        tc = TimeControl(depth=args.depth)
    elif args.wtime or args.btime:
        tc = TimeControl(
            wtime=args.wtime, btime=args.btime,
            winc=args.winc,   binc=args.binc,
            movestogo=args.movestogo,
        )
    else:
        # Default fallback
        tc = TimeControl(movetime_ms=500)
        print("[INFO] No time control specified, defaulting to --movetime 500")

    # Labels
    p1_name = lambda threads: os.path.splitext(os.path.basename(args.engine1))[0]
    p2_name = lambda threads: os.path.splitext(os.path.basename(engine2_path))[0]

    p1_label = args.p1_label or f"{p1_name(args.p1_threads)} [{args.p1_threads}T]"
    p2_label = args.p2_label or f"{p2_name(args.p2_threads)} [{args.p2_threads}T]"

    # Same binary? Differentiate labels
    if args.engine1 == engine2_path and not (args.p1_label or args.p2_label):
        p2_label = p2_label.replace("]", ", P2]")
        p1_label = p1_label.replace("]", ", P1]")

    print(f"Starting engines...")
    print(f"  P1: {p1_label}  ({args.engine1})")
    print(f"  P2: {p2_label}  ({engine2_path})")

    engine1 = UCIEngine(args.engine1, threads=args.p1_threads, hash_mb=args.p1_hash)
    engine2 = UCIEngine(engine2_path, threads=args.p2_threads, hash_mb=args.p2_hash)

    p1 = UCIPlayer(engine1, label=p1_label)
    p2 = UCIPlayer(engine2, label=p2_label)

    verbose = not args.quiet

    # Optional board display hook
    if args.board:
        board_hook = _make_board_hook()
    else:
        board_hook = None

    try:
        if board_hook:
            # Wrap the default per-move output with board display
            original_make_cb = MatchRunner._make_move_callback

            def patched_make_cb(self_runner, verbose_, game_idx):
                inner = original_make_cb(self_runner, verbose_, game_idx)
                def combined(ev: MoveEvent):
                    if inner:
                        inner(ev)
                    board_hook(ev)
                return combined

            MatchRunner._make_move_callback = patched_make_cb

        config = MatchConfig(
            games        = args.games,
            tc           = tc,
            swap_colors  = not args.no_swap,
            starting_fen = args.fen,
            pgn_path     = args.pgn,
            verbose      = verbose,
            annotate     = not args.no_annotate,
            event_name   = "Self-play",
        )

        runner = MatchRunner(p1, p2, config)
        runner.run()

    except KeyboardInterrupt:
        print("\n[Interrupted]")

    finally:
        engine1.close()
        engine2.close()
        if verbose:
            print("Engines shut down.")


def _make_board_hook():
    """Returns an on_move callback that prints the ASCII board."""
    def hook(ev: MoveEvent):
        print(ev.board.ascii())
        print()
    return hook


if __name__ == "__main__":
    main()
