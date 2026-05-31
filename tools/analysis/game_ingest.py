#!/usr/bin/env python3
"""
game_ingest.py â€” Stage 1 of the game analysis pipeline.

Reads annotated PGN files from data/games/ (produced by the bot's gameDb.js),
extracts every bot-move position with its stored engine eval, then flags
positions where the eval swings significantly between our consecutive turns â€”
these are the positions most worth diagnosing.

Outputs a JSON file of position records, ready for game_diagnose.py.

Usage:
    # Ingest the current month
    python tools/game_ingest.py

    # Specific month
    python tools/game_ingest.py --month 2026-03

    # All months
    python tools/game_ingest.py --all

    # Custom threshold (default 80cp) and output tag
    python tools/game_ingest.py --threshold 80 --tag march_review

    # Also include all our moves from losing games (not just big swings)
    python tools/game_ingest.py --include-losses

Output:
    results/analysis/<tag>_positions.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.dirname(os.path.dirname(_TOOLS_DIR))
_GAMES_DIR = os.path.join(_ROOT, "data", "games")
_OUT_DIR   = os.path.join(_ROOT, "results", "analysis")

try:
    import chess
    import chess.pgn
except ImportError:
    sys.exit("chess library required: pip install chess")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PositionRecord:
    """One position of interest extracted from a played game."""

    # Identity
    game_id:      str
    source_file:  str
    service:      str
    date:         str
    time_control: str
    speed:        str
    rated:        bool
    result:       str           # "1-0" / "0-1" / "1/2-1/2"
    bot_color:    str           # "white" / "black"
    opponent:     str

    # Position
    ply:          int           # half-move (1-indexed)
    fen_before:   str           # FEN before bot produced this move
    our_move_uci: str           # move the bot actually played (UCI)
    stored_eval:  Optional[float]   # white-relative eval from %eval annotation
    eval_for_us:  Optional[float]   # stored_eval flipped to bot's perspective
    depth:        int
    nodes:        int
    time_ms:      float

    # Context for significance scoring
    swing_cp:     Optional[float]   # cp drop vs our PREVIOUS eval (for us)
    phase:        str           # "opening" / "middlegame" / "endgame"
    material:     int           # approximate remaining centipawn material
    flagged:      bool          # True = anchor (selected) / False = context window
    flag_reason:  str           # why it was flagged (e.g. "swing", "worst3", "loss")

    # Sequence window bookkeeping
    window_role:  str = "anchor"  # "anchor" or "context"
    anchor_ply:   int = 0         # ply of the anchor this position belongs to


# ---------------------------------------------------------------------------
# PGN parsing helpers
# ---------------------------------------------------------------------------

_EVAL_RE  = re.compile(r'%eval\s+(#[-âˆ’]?\d+|[-âˆ’]?\d+(?:\.\d+)?)', re.I)
_DEPTH_RE = re.compile(r'%depth\s+(\d+)', re.I)
_NODES_RE = re.compile(r'%nodes\s+(\d+)', re.I)
_TIME_RE  = re.compile(r'%time\s+(\d+(?:\.\d+)?)', re.I)


def _parse_comment(comment: str) -> dict:
    """Extract %eval, %depth, %nodes, %time from a PGN move comment."""
    result = {"eval": None, "depth": 0, "nodes": 0, "time_ms": 0.0}

    m = _EVAL_RE.search(comment or "")
    if m:
        raw = m.group(1).replace("âˆ’", "-")
        if raw.startswith("#"):
            # Mate score: #3 â†’ +300 synthetic cp; #-3 â†’ -300
            n = int(raw[1:])
            result["eval"] = (300.0 if n > 0 else -300.0)
        else:
            result["eval"] = float(raw)

    m = _DEPTH_RE.search(comment or "")
    if m:
        result["depth"] = int(m.group(1))

    m = _NODES_RE.search(comment or "")
    if m:
        result["nodes"] = int(m.group(1))

    m = _TIME_RE.search(comment or "")
    if m:
        result["time_ms"] = float(m.group(1)) * 1000

    return result


def _game_phase(board: chess.Board) -> str:
    """Classify position by game phase."""
    ply = board.ply()
    if ply < 15:
        return "opening"
    # Count major + minor pieces (not pawns/kings)
    piece_count = len(board.pieces(chess.QUEEN,  chess.WHITE)) + \
                  len(board.pieces(chess.QUEEN,  chess.BLACK)) + \
                  len(board.pieces(chess.ROOK,   chess.WHITE)) + \
                  len(board.pieces(chess.ROOK,   chess.BLACK)) + \
                  len(board.pieces(chess.BISHOP, chess.WHITE)) + \
                  len(board.pieces(chess.BISHOP, chess.BLACK)) + \
                  len(board.pieces(chess.KNIGHT, chess.WHITE)) + \
                  len(board.pieces(chess.KNIGHT, chess.BLACK))
    if piece_count <= 4:
        return "endgame"
    return "middlegame"


_PIECE_VALUES = {
    chess.PAWN: 100, chess.KNIGHT: 325, chess.BISHOP: 325,
    chess.ROOK: 500, chess.QUEEN: 975, chess.KING: 0,
}

def _material_cp(board: chess.Board) -> int:
    total = 0
    for piece_type, value in _PIECE_VALUES.items():
        total += value * (len(board.pieces(piece_type, chess.WHITE)) +
                          len(board.pieces(piece_type, chess.BLACK)))
    return total


def _eval_for_us(white_relative: float, bot_color: str) -> float:
    return white_relative if bot_color == "white" else -white_relative


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def extract_positions(
    pgn_path: str,
    threshold_cp: float = 80.0,
    include_losses: bool = False,
    worst_n: Optional[int] = None,
    window: int = 0,
) -> list[PositionRecord]:
    """
    Parse one annotated PGN file and return a list of PositionRecord objects.

    Anchor selection (flagged=True positions):
      - eval_for_us drops â‰¥ threshold_cp vs our PREVIOUS eval on the same turn
      - worst_n: always flag the N moves with the largest eval drop per game,
        regardless of absolute threshold (ensures every game contributes anchors)
      - include_losses: flag all bot moves from games we lost

    Window expansion (flagged=False positions):
      - If window > 0, also include `window` bot-move positions before and after
        each anchor, marked window_role='context'. Provides the arc of how the
        position degraded / recovered around each blunder.
    """
    records: list[PositionRecord] = []
    source = os.path.basename(pgn_path)

    with open(pgn_path, encoding="utf-8", errors="replace") as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break

            headers   = game.headers
            game_id   = headers.get("GameId", "unknown")
            service   = headers.get("Service",     "unknown")
            date      = headers.get("Date",        "????")
            tc        = headers.get("TimeControl", "?")
            speed     = headers.get("Speed",       "?")
            rated     = headers.get("Event",       "").lower() != "casual"
            result    = headers.get("Result",      "*")
            white     = headers.get("White",       "?")
            # Bot is always "Bot" in White or Black tag
            bot_color = "white" if white == "Bot" else "black"
            opponent  = headers.get("Black" if bot_color == "white" else "White", "?")

            game_lost = (result == "0-1" and bot_color == "white") or \
                        (result == "1-0" and bot_color == "black")

            board = game.board()
            node  = game.variation(0) if game.variations else None

            # Collect all our moves with evals
            our_evals: list[list] = []  # [(ply, eval_for_us)]
            move_data:  list[dict] = []

            cur_node = game
            while cur_node.variations:
                next_node = cur_node.variations[0]
                move      = next_node.move
                ply       = board.ply() + 1  # 1-indexed half-moves
                comment   = next_node.comment or ""
                parsed    = _parse_comment(comment)
                is_our    = (board.turn == chess.WHITE and bot_color == "white") or \
                            (board.turn == chess.BLACK and bot_color == "black")

                if is_our:
                    fen_before = board.fen()
                    phase      = _game_phase(board)
                    material   = _material_cp(board)
                    our_eval_raw = parsed["eval"]
                    our_eval_us  = _eval_for_us(our_eval_raw, bot_color) if our_eval_raw is not None else None

                    move_data.append({
                        "ply":       ply,
                        "fen":       fen_before,
                        "uci":       move.uci(),
                        "eval_raw":  our_eval_raw,
                        "eval_us":   our_eval_us,
                        "depth":     parsed["depth"],
                        "nodes":     parsed["nodes"],
                        "time_ms":   parsed["time_ms"],
                        "phase":     phase,
                        "material":  material,
                    })

                board.push(move)
                cur_node = next_node

            # â”€â”€ Compute swing for every bot move â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            for i, md in enumerate(move_data):
                swing = None
                if i > 0 and md["eval_us"] is not None and move_data[i-1]["eval_us"] is not None:
                    swing = md["eval_us"] - move_data[i-1]["eval_us"]
                md["swing"] = swing

            # â”€â”€ Determine anchor (flagged) indices â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # Maps move_data index â†’ flag_reason string
            anchor_set: dict[int, str] = {}

            # 1. Threshold-based swing (any single catastrophic drop)
            for i, md in enumerate(move_data):
                if md["swing"] is not None and md["swing"] <= -threshold_cp:
                    anchor_set[i] = f"swing:{md['swing']:+.0f}cp"

            # 2. Per-game worst-N: always flag the N moves with the worst drop,
            #    ensuring every game contributes regardless of threshold.
            if worst_n is not None and worst_n > 0:
                scored = [(i, md["swing"]) for i, md in enumerate(move_data)
                          if md["swing"] is not None]
                scored.sort(key=lambda x: x[1])   # most negative first
                for i, sw in scored[:worst_n]:
                    if i not in anchor_set:
                        anchor_set[i] = f"worst{worst_n}:{sw:+.0f}cp"

            # 3. All moves from losing games (dense but low-signal corpus)
            if include_losses and game_lost:
                for i in range(len(move_data)):
                    if i not in anchor_set:
                        anchor_set[i] = "loss"

            if not anchor_set:
                continue   # no flagged positions in this game â€” skip to next

            # â”€â”€ Expand each anchor by Â±window bot-move positions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # included[i] = (is_anchor, anchor_ply_value, flag_reason)
            included: dict[int, tuple[bool, int, str]] = {}

            for anchor_i, reason in anchor_set.items():
                aply = move_data[anchor_i]["ply"]
                # Anchor itself (may already be in dict as context; anchor wins)
                if anchor_i not in included or not included[anchor_i][0]:
                    included[anchor_i] = (True, aply, reason)
                # Context window
                for offset in range(-window, window + 1):
                    if offset == 0:
                        continue
                    j = anchor_i + offset
                    if j < 0 or j >= len(move_data):
                        continue
                    if j not in included:   # don't overwrite an anchor
                        included[j] = (False, aply, "context")

            # â”€â”€ Emit one PositionRecord per collected index â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            for i in sorted(included):
                md = move_data[i]
                is_anchor, anchor_ply_val, flag_reason = included[i]
                records.append(PositionRecord(
                    game_id      = game_id,
                    source_file  = source,
                    service      = service,
                    date         = date,
                    time_control = tc,
                    speed        = speed,
                    rated        = rated,
                    result       = result,
                    bot_color    = bot_color,
                    opponent     = opponent,
                    ply          = md["ply"],
                    fen_before   = md["fen"],
                    our_move_uci = md["uci"],
                    stored_eval  = md["eval_raw"],
                    eval_for_us  = md["eval_us"],
                    depth        = md["depth"],
                    nodes        = md["nodes"],
                    time_ms      = md["time_ms"],
                    swing_cp     = md["swing"],
                    phase        = md["phase"],
                    material     = md["material"],
                    flagged      = is_anchor,
                    flag_reason  = flag_reason,
                    window_role  = "anchor" if is_anchor else "context",
                    anchor_ply   = anchor_ply_val,
                ))

    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract interesting positions from played bot games."
    )
    p.add_argument("--month",     metavar="YYYY-MM",
                   help="Process a specific month (e.g. 2026-03)")
    p.add_argument("--all",       action="store_true",
                   help="Process all PGN files in data/games/")
    p.add_argument("--file",      metavar="PATH",
                   help="Process a specific PGN file")
    p.add_argument("--threshold", type=float, default=80.0,
                   help="Eval-swing threshold in centipawns (default: 80)")
    p.add_argument("--worst-n",  type=int, default=None, metavar="N",
                   help="Always flag the N worst moves per game by eval drop, "
                        "ensuring every game contributes anchors (e.g. --worst-n 3)")
    p.add_argument("--window",   type=int, default=0, metavar="W",
                   help="Include W context positions before/after each anchor (default: 0); "
                        "e.g. --window 2 gives a 5-move arc per blunder")
    p.add_argument("--include-losses", action="store_true",
                   help="Also include all bot moves from losing games")
    p.add_argument("--tag",       default="",
                   help="Output tag for result file (default: derived from source)")
    p.add_argument("--out-dir",   default=_OUT_DIR,
                   help="Output directory (default: results/analysis/)")
    p.add_argument("--quiet",     action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # Collect PGN files to process
    pgn_files = []
    if args.file:
        pgn_files = [args.file]
    elif args.month:
        pgn_files = [os.path.join(_GAMES_DIR, f"{args.month}.pgn")]
    elif args.all:
        pgn_files = sorted(Path(_GAMES_DIR).glob("*.pgn"))
        pgn_files = [str(f) for f in pgn_files]
    else:
        # Default: current month
        month = datetime.utcnow().strftime("%Y-%m")
        pgn_files = [os.path.join(_GAMES_DIR, f"{month}.pgn")]

    all_records: list[PositionRecord] = []

    for pgn_path in pgn_files:
        if not os.path.exists(pgn_path):
            print(f"[ingest] skip {pgn_path} (not found)")
            continue
        if not args.quiet:
            print(f"[ingest] reading {pgn_path} â€¦", flush=True)
        recs = extract_positions(
            pgn_path,
            threshold_cp=args.threshold,
            include_losses=args.include_losses,
            worst_n=args.worst_n,
            window=args.window,
        )
        anchors_here = sum(1 for r in recs if r.flagged)
        context_here = len(recs) - anchors_here
        if not args.quiet:
            if args.window > 0:
                print(f"         â†’ {anchors_here} anchors + {context_here} context = {len(recs)} positions")
            else:
                print(f"         â†’ {len(recs)} positions flagged")
        all_records.extend(recs)

    if not all_records:
        print("[ingest] No flagged positions found. Try --worst-n 3, --include-losses, or lower --threshold.")
        return

    # Determine output file
    if args.tag:
        tag = args.tag
    elif args.month:
        tag = args.month
    elif args.all:
        tag = "all"
    else:
        tag = datetime.utcnow().strftime("%Y-%m")

    anchors  = [r for r in all_records if r.flagged]
    context  = [r for r in all_records if not r.flagged]

    out_path = os.path.join(args.out_dir, f"{tag}_positions.json")
    data = {
        "tag":            tag,
        "threshold_cp":   args.threshold,
        "worst_n":        args.worst_n,
        "window":         args.window,
        "include_losses": args.include_losses,
        "generated":      datetime.utcnow().isoformat(),
        "total":          len(all_records),
        "anchors":        len(anchors),
        "context":        len(context),
        "positions":      [asdict(r) for r in all_records],
    }
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n[ingest] {len(anchors)} anchors + {len(context)} context = {len(all_records)} total â†’ {out_path}")

    # Phase breakdown (anchors only â€” context would skew the numbers)
    phases = {}
    for r in anchors:
        phases[r.phase] = phases.get(r.phase, 0) + 1
    print("  anchors by phase:")
    for ph, n in sorted(phases.items()):
        print(f"    {ph:12s}: {n}")

    # Flag reason summary
    reasons: dict[str, int] = {}
    for r in anchors:
        key = r.flag_reason.split(":")[0]   # "swing", "worst3", "loss"
        reasons[key] = reasons.get(key, 0) + 1
    print("  anchor flag reasons:")
    for rsn, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {rsn:16s}: {n}")

    print(f"\nNext step: python tools/game_diagnose.py --input {out_path}")


if __name__ == "__main__":
    main()
