#!/usr/bin/env python3
"""
replay_analysis.py â€” Forensic replay of a bot's game with engine analysis.

Replays a game from a PGN file. At each position where the bot was to move,
the engine re-analyses the position and compares what it finds against what
the bot actually played. This reveals time-pressure blunders, missed mates,
and positions where the search found the right move but played the wrong one.

Usage examples
--------------
  # List all games in a PGN:
    python tools/replay_analysis.py analyze/games.pgn --list

  # Analyse a specific game (Lichess ID from [Site] tag):
    python tools/replay_analysis.py analyze/games.pgn --game mNnTB2Ca

  # Also analyse opponent moves:
    python tools/replay_analysis.py analyze/games.pgn --game mNnTB2Ca --both-sides

  # Use depth instead of movetime:
    python tools/replay_analysis.py analyze/games.pgn --game mNnTB2Ca --depth 25

  # Only flag mistakes >= 100 cp (default 50):
    python tools/replay_analysis.py analyze/games.pgn --game mNnTB2Ca --threshold 100

  # Show only blunders (no board, compact output):
    python tools/replay_analysis.py analyze/games.pgn --game mNnTB2Ca --compact

Options
-------
  --game <id>         Lichess game ID (from [Site] tag) or move number (1-based).
                      Omit to analyse the first game in the file.
  --bot <name>        Bot's username (default: auto-detect from White/Black tags).
  --both-sides        Analyse both players' moves, not just the bot's.
  --movetime <ms>     Milliseconds per position (default: 1000).
  --depth <n>         Use depth limit instead of movetime.
  --threads <n>       Engine threads (default: 4).
  --hash <mb>         Engine hash table size in MB (default: 128).
  --threshold <cp>    Only flag moves worse than engine's top by this many cp
                      (default: 50). Moves within threshold shown in dim green.
  --engine <path>     Path to UCI engine executable (auto-detected if omitted).
  --compact           Skip board rendering; show only move table.
  --list              List all games in the PGN and exit.
  --no-color          Disable ANSI colour output.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from typing import Optional

# Force UTF-8 output on Windows so Unicode pieces/box-drawing chars work
if sys.platform == "win32":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import chess
import chess.pgn

# Ensure tools/uci is importable
_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_TOOLS = _ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from uci.engine import UCIEngine, SearchResult

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------

try:
    import ctypes as _ctypes
    _ctypes.windll.kernel32.SetConsoleMode(
        _ctypes.windll.kernel32.GetStdHandle(-11), 7
    )
except Exception:
    pass

_NO_COLOR = False

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t):   return _c("92", t)
def yellow(t):  return _c("93", t)
def red(t):     return _c("91", t)
def cyan(t):    return _c("96", t)
def dim(t):     return _c("2",  t)
def bold(t):    return _c("1",  t)
def magenta(t): return _c("95", t)

# ---------------------------------------------------------------------------
# Engine discovery
# ---------------------------------------------------------------------------

sys.path.insert(0, str(_ROOT / "tools"))
from engine_config import find_engine as _find_bot_engine

_SF_CANDIDATES = [
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe",
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64.exe",
    "engines/stockfish/stockfish.exe",
]

def find_engine(hint: Optional[str] = None) -> str:
    if hint:
        if os.path.isfile(hint):
            return os.path.abspath(hint)
        raise FileNotFoundError(f"Engine not found: {hint!r}")
    # Try our engine first, then SF
    try:
        return _find_bot_engine()
    except FileNotFoundError:
        pass
    for rel in _SF_CANDIDATES:
        p = _ROOT / rel
        if p.exists():
            return str(p)
    raise FileNotFoundError(
        "No engine found. Specify --engine <path> or build the project."
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_str(result: SearchResult) -> str:
    """Human-readable score from the engine's perspective (side to move)."""
    if result.score_mate is not None:
        m = result.score_mate
        if m > 0:
            return green(f"+M{m}")
        elif m < 0:
            return red(f"-M{abs(m)}")
        else:
            return "M0"
    if result.score_cp is not None:
        cp = result.score_cp
        if cp > 0:
            colour = green if cp >= 100 else yellow
            return colour(f"+{cp/100:.2f}")
        elif cp < 0:
            colour = red if cp <= -100 else yellow
            return colour(f"{cp/100:.2f}")
        else:
            return dim("0.00")
    return "?"


def _score_cp_normalized(result: SearchResult) -> Optional[int]:
    """
    Return an integer centipawn score normalized so that higher is always
    better for the side that just moved (i.e. we negate when it's Black's turn
    and the engine score is from side-to-move's perspective).
    Actually the engine gives score from side-to-move. We keep as-is.
    Returns None for mate scores (handled separately).
    """
    if result.score_cp is not None:
        return result.score_cp
    if result.score_mate is not None:
        # Approximate: mate in 1 â‰ˆ 10000, further mates proportionally less
        m = result.score_mate
        if m > 0:
            return 10000 - m * 10
        else:
            return -10000 + abs(m) * 10
    return None


def _pv_uci_to_san(board: chess.Board, pv_uci: str, max_moves: int = 5) -> str:
    """Convert a PV line from UCI notation to SAN for display."""
    tokens = pv_uci.strip().split()
    tmp = board.copy()
    san_parts = []
    for tok in tokens[:max_moves]:
        try:
            move = chess.Move.from_uci(tok)
            san_parts.append(tmp.san(move))
            tmp.push(move)
        except Exception:
            break
    return " ".join(san_parts)


def _render_board(board: chess.Board, last_move: Optional[chess.Move] = None,
                  flip: bool = False) -> str:
    """Render the board with ANSI colours. flip=True shows from Black's side."""
    if _NO_COLOR:
        return str(board)

    LIGHT_BG = "\033[48;5;223m"
    DARK_BG  = "\033[48;5;130m"
    HL_BG    = "\033[48;5;184m"  # highlighted (last move)
    RESET    = "\033[0m"
    W_PIECE  = "\033[97m"
    B_PIECE  = "\033[30m"

    UNICODE = {
        "P": "â™™", "N": "â™˜", "B": "â™—", "R": "â™–", "Q": "â™•", "K": "â™”",
        "p": "â™Ÿ", "n": "â™ž", "b": "â™", "r": "â™œ", "q": "â™›", "k": "â™š",
    }

    hl_squares = set()
    if last_move:
        hl_squares.add(last_move.from_square)
        hl_squares.add(last_move.to_square)

    ranks = range(7, -1, -1) if not flip else range(0, 8)
    files = range(0, 8) if not flip else range(7, -1, -1)

    lines = []
    for r in ranks:
        row = f" {r+1} "
        for f in files:
            sq = chess.square(f, r)
            bg = HL_BG if sq in hl_squares else (LIGHT_BG if (r + f) % 2 == 1 else DARK_BG)
            piece = board.piece_at(sq)
            if piece:
                sym = UNICODE.get(piece.symbol(), piece.symbol())
                fg = W_PIECE if piece.color == chess.WHITE else B_PIECE
                row += f"{bg}{fg} {sym} {RESET}"
            else:
                row += f"{bg}   {RESET}"
        lines.append(row)

    file_labels = "   " + "  ".join(" abcdefgh"[f+1] for f in files)
    lines.append(file_labels)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PGN loading
# ---------------------------------------------------------------------------

def load_game(pgn_path: str, game_selector: Optional[str]) -> chess.pgn.Game:
    """Load a game from a PGN file by Lichess ID, index (1-based), or first."""
    with open(pgn_path, encoding="utf-8", errors="replace") as f:
        idx = 0
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            idx += 1
            if game_selector is None:
                return game
            # Match by Lichess game ID in Site header
            site = game.headers.get("Site", "")
            if game_selector in site:
                return game
            # Match by 1-based index
            try:
                if int(game_selector) == idx:
                    return game
            except ValueError:
                pass

    selector_desc = (f"game '{game_selector}'" if game_selector else "any game")
    raise ValueError(f"No {selector_desc} found in {pgn_path!r}")


def list_games(pgn_path: str):
    """Print a table of all games in a PGN file."""
    with open(pgn_path, encoding="utf-8", errors="replace") as f:
        idx = 0
        print(bold(f"{'#':>4}  {'Site/ID':<24}  {'White':<20}  {'Black':<20}  "
                   f"{'Result':>6}  {'TC':<10}  Termination"))
        print(dim("â”€" * 105))
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            idx += 1
            headers = game.headers
            site = headers.get("Site", "?")
            gid  = site.split("/")[-1] if "/" in site else site
            white = headers.get("White", "?")[:20]
            black = headers.get("Black", "?")[:20]
            result = headers.get("Result", "?")
            tc = headers.get("TimeControl", "?")[:10]
            term = headers.get("Termination", "?")[:30]
            rstr = green(result) if result == "1-0" else red(result) if result == "0-1" else yellow(result)
            print(f"  {idx:>3}  {gid:<24}  {white:<20}  {black:<20}  {rstr:>6}  {tc:<10}  {dim(term)}")


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyse_game(
    game: chess.pgn.Game,
    engine: UCIEngine,
    bot_name: Optional[str],
    both_sides: bool,
    movetime_ms: Optional[int],
    depth: Optional[int],
    threshold_cp: int,
    compact: bool,
):
    headers = game.headers
    white = headers.get("White", "?")
    black = headers.get("Black", "?")
    result = headers.get("Result", "*")
    site   = headers.get("Site", "?")
    gid    = site.split("/")[-1] if "/" in site else site
    tc     = headers.get("TimeControl", "?")
    opening = headers.get("Opening", headers.get("ECOName", "?"))
    eco     = headers.get("ECO", "?")

    # Determine bot's colour
    if bot_name:
        if white.lower() == bot_name.lower():
            bot_color = chess.WHITE
        elif black.lower() == bot_name.lower():
            bot_color = chess.BLACK
        else:
            # Try partial match
            if bot_name.lower() in white.lower():
                bot_color = chess.WHITE
            elif bot_name.lower() in black.lower():
                bot_color = chess.BLACK
            else:
                print(yellow(f"Warning: '{bot_name}' not found in White='{white}' or Black='{black}'."))
                bot_color = None  # analyse both
    else:
        bot_color = None  # no bot specified â†’ analyse the side that lost, or White

    print()
    print(bold("=" * 72))
    print(bold(f"  GAME REPLAY ANALYSIS"))
    print(bold("=" * 72))
    print(f"  Game   : {cyan(gid)}  ({site})")
    print(f"  White  : {bold(white) if bot_color == chess.WHITE else white}")
    print(f"  Black  : {bold(black) if bot_color == chess.BLACK else black}")
    print(f"  Result : {green(result) if result=='1-0' else red(result) if result=='0-1' else yellow(result)}")
    print(f"  TC     : {tc}   Opening : {eco} {opening}")

    if movetime_ms:
        analysis_mode = f"movetime {movetime_ms} ms"
    elif depth:
        analysis_mode = f"depth {depth}"
    else:
        analysis_mode = "movetime 1000 ms (default)"

    colname = "White" if bot_color == chess.WHITE else "Black" if bot_color == chess.BLACK else "Both"
    print(f"  Analysing : {bold(colname)} moves â€” {analysis_mode} â€” threshold {threshold_cp} cp")
    print()

    engine.new_game()

    board = game.board()
    node  = game.next()  # first move node

    move_num     = 0
    blunders     = []   # (move_num_str, played_san, best_san, delta_cp)
    missed_mates = []

    bar_width = 40  # for progress display

    while node:
        move      = node.move
        san       = node.san()
        move_color = not board.turn  # board.turn is next to move; this move WAS played by the other colour
        # Actually board.turn is who moved: before push, board.turn == who is making this move
        mover_color = board.turn

        # Decide whether to analyse this move
        do_analyse = both_sides or (bot_color is None) or (mover_color == bot_color)

        move_num += 1
        full_move = board.fullmove_number
        side_str  = "w" if mover_color == chess.WHITE else "b"

        if mover_color == chess.WHITE:
            move_label = f"{full_move}."
        else:
            move_label = f"{full_move}..."

        if do_analyse:
            # â”€â”€ Give the engine this position â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            fen = board.fen()
            engine.position(fen)

            t0 = time.monotonic()
            if movetime_ms:
                result_sr = engine.go(movetime_ms=movetime_ms)
            elif depth:
                result_sr = engine.go(depth=depth)
            else:
                result_sr = engine.go(movetime_ms=1000)
            elapsed = time.monotonic() - t0

            best_uci = result_sr.bestmove
            try:
                best_move = chess.Move.from_uci(best_uci) if best_uci else None
                best_san  = board.san(best_move) if best_move else "?"
            except Exception:
                best_san  = best_uci or "?"
                best_move = None

            played_san = san  # SAN before the move is pushed

            # Score: positive = good for side to move
            engine_cp  = _score_cp_normalized(result_sr)
            pv_san     = _pv_uci_to_san(board, result_sr.pv, max_moves=6)

            # Did the bot play the engine's best move?
            played_uci = move.uci()
            matched    = (played_uci == best_uci)

            # Score delta: if bot played a different move, after pushing the bot's
            # move, re-evaluate to see how much worse it is.
            delta_cp = 0
            eval_after_str = ""
            if not matched and best_move is not None:
                # Quick re-eval of bot's actual move at same depth/time
                board_after = board.copy()
                board_after.push(move)
                engine.position(board_after.fen())
                # Use same time/depth but halved for speed in delta check
                if movetime_ms:
                    re_sr = engine.go(movetime_ms=max(movetime_ms // 2, 200))
                elif depth:
                    re_sr = engine.go(depth=max(depth - 4, 6))
                else:
                    re_sr = engine.go(movetime_ms=500)
                # After the bot's move the engine score is from the opponent's perspective
                # Negate to get score from bot's perspective loss
                re_cp = _score_cp_normalized(re_sr)
                if re_cp is not None:
                    # re_cp is from opponent's perspective (they move next)
                    # so bot's eval after their move = -re_cp
                    bot_eval_after = -re_cp
                    if engine_cp is not None:
                        delta_cp = engine_cp - bot_eval_after
                    eval_after_str = f"  â†’after played: {_score_str(re_sr)} (from opp)"

            # -- Classification --
            if result_sr.score_mate is not None and result_sr.score_mate > 0 and not matched:
                classification = red("MISSED MATE")
                missed_mates.append((f"{move_label}{played_san}", best_san,
                                     result_sr.score_mate))
            elif delta_cp >= threshold_cp:
                if delta_cp >= 300:
                    classification = red(f"BLUNDER  Î”{delta_cp:+d}cp")
                elif delta_cp >= 150:
                    classification = yellow(f"MISTAKE  Î”{delta_cp:+d}cp")
                else:
                    classification = yellow(f"INACCURACY Î”{delta_cp:+d}cp")
                blunders.append((f"{move_label}{played_san}", best_san, delta_cp))
            elif matched:
                classification = green("âœ“ best")
            else:
                classification = dim(f"~ok (Î”{delta_cp:+d}cp)")

            # â”€â”€ Render â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if not compact:
                flip = (mover_color == chess.BLACK)
                last_highlight = move
                print(bold(f"â”€â”€ {move_label} {played_san} {'(bot)' if mover_color == bot_color else '(opp)'} "
                           f"{'â”€'*50}"))
                print(_render_board(board, last_move=None, flip=flip))
                print()

            # Move line
            match_glyph = "âœ“" if matched else "âœ—"
            match_colour = green if matched else (red if delta_cp >= 150 else yellow)
            played_fmt = (green if matched else (red if delta_cp >= 300 else yellow))(played_san)
            best_fmt   = (cyan(best_san) if not matched else dim(best_san))

            depth_str  = f"d{result_sr.depth}/{result_sr.seldepth}"
            nps_str    = f"{result_sr.nps/1000:.0f}kn/s" if result_sr.nps else ""
            time_str   = f"{elapsed*1000:.0f}ms"
            score_fmt  = _score_str(result_sr)

            if compact:
                # Single-line compact format
                status = classification
                print(f"  {move_label:<6} {played_fmt:<12} engine:{best_fmt:<12} "
                      f"score:{score_fmt:<10} {depth_str:<8} {status}")
            else:
                print(f"  Played  : {played_fmt}  {'(= engine best)' if matched else ''}")
                print(f"  Engine  : {best_fmt}   score {score_fmt}   {depth_str}  {nps_str}  {time_str}")
                if pv_san:
                    print(f"  PV      : {dim(pv_san)}")
                if eval_after_str:
                    print(f"  Delta   :{eval_after_str}")
                print(f"  Verdict : {classification}")
                print()

        else:
            # Opponent move â€” just show it dimly in compact mode
            if compact and not both_sides:
                # Don't print anything for not-analysed moves in compact
                pass
            elif not compact:
                print(dim(f"â”€â”€ {move_label} {san} (opp) â”€â”€â”€â”€â”€â”€"))

        board.push(move)
        node = node.next()

    # â”€â”€ Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print()
    print(bold("=" * 72))
    print(bold("  SUMMARY"))
    print(bold("=" * 72))
    total_bot_moves = sum(
        1 for i, nd in enumerate(game.mainline())
        if (((i % 2 == 0) and bot_color == chess.WHITE) or
            ((i % 2 == 1) and bot_color == chess.BLACK) or
            bot_color is None)
    )
    print(f"  Missed mates : {red(str(len(missed_mates))) if missed_mates else green('0')}")
    print(f"  Blunders (â‰¥300cp) : {red(str(sum(1 for _,_,d in blunders if d>=300)))}")
    print(f"  Mistakes (â‰¥150cp) : {yellow(str(sum(1 for _,_,d in blunders if 150<=d<300)))}")
    print(f"  Inaccuracies (â‰¥{threshold_cp}cp) : {yellow(str(sum(1 for _,_,d in blunders if threshold_cp<=d<150)))}")

    if missed_mates:
        print()
        print(bold("  Missed mates:"))
        for mv, best, n in missed_mates:
            print(f"    {red(mv)} â†’ engine had {cyan(best)} (mate in {n})")

    if blunders:
        print()
        print(bold(f"  Errors (â‰¥{threshold_cp}cp):"))
        for mv, best, delta in sorted(blunders, key=lambda x: -x[2]):
            colour = red if delta >= 300 else yellow
            print(f"    {colour(mv):<20} â†’ engine: {cyan(best):<12}  Î”{delta:+d}cp")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global _NO_COLOR

    ap = argparse.ArgumentParser(
        description="Replay a bot game with engine analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("pgn", help="Path to PGN file")
    ap.add_argument("--game", default=None,
                    help="Lichess game ID or 1-based game number")
    ap.add_argument("--bot", default="H-035",
                    help="Bot username to analyse (default: H-035)")
    ap.add_argument("--both-sides", action="store_true",
                    help="Analyse both sides' moves")
    ap.add_argument("--movetime", type=int, default=None,
                    help="Milliseconds per position (default: 1000)")
    ap.add_argument("--depth", type=int, default=None,
                    help="Depth limit instead of movetime")
    ap.add_argument("--threads", type=int, default=4,
                    help="Engine threads (default: 4)")
    ap.add_argument("--hash", dest="hash_mb", type=int, default=128,
                    help="Engine hash in MB (default: 128)")
    ap.add_argument("--threshold", type=int, default=50,
                    help="Min cp loss to flag as inaccuracy (default: 50)")
    ap.add_argument("--engine", default=None,
                    help="Path to UCI engine executable")
    ap.add_argument("--compact", action="store_true",
                    help="Compact output: no board, one line per move")
    ap.add_argument("--list", action="store_true",
                    help="List all games in the PGN and exit")
    ap.add_argument("--no-color", dest="no_color", action="store_true",
                    help="Disable ANSI colour output")

    args = ap.parse_args()
    _NO_COLOR = args.no_color

    pgn_path = args.pgn
    if not os.path.isfile(pgn_path):
        # Try relative to workspace root
        alt = _ROOT / pgn_path
        if alt.exists():
            pgn_path = str(alt)
        else:
            print(red(f"Error: PGN file not found: {pgn_path!r}"), file=sys.stderr)
            sys.exit(1)

    if args.list:
        list_games(pgn_path)
        return

    # Load the game
    try:
        game = load_game(pgn_path, args.game)
    except ValueError as e:
        print(red(f"Error: {e}"), file=sys.stderr)
        sys.exit(1)

    # Find engine
    try:
        engine_path = find_engine(args.engine)
    except FileNotFoundError as e:
        print(red(f"Error: {e}"), file=sys.stderr)
        sys.exit(1)

    print(dim(f"Engine : {engine_path}"))
    print(dim(f"PGN    : {pgn_path}"))

    # Use movetime default if neither specified
    movetime = args.movetime
    if movetime is None and args.depth is None:
        movetime = 1000

    with UCIEngine(engine_path, threads=args.threads, hash_mb=args.hash_mb) as engine:
        analyse_game(
            game         = game,
            engine       = engine,
            bot_name     = args.bot,
            both_sides   = args.both_sides,
            movetime_ms  = movetime,
            depth        = args.depth,
            threshold_cp = args.threshold,
            compact      = args.compact,
        )


if __name__ == "__main__":
    main()
