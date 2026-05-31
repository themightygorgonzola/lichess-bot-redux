#!/usr/bin/env python3
"""
game_diagnose.py â€” Stage 2 of the game analysis pipeline.

For each position extracted by game_ingest.py, runs a deep probe:
  - Our engine at 3 time tiers (500ms, 2000ms, 5000ms)
  - Stockfish at 2000ms for reference
  - Captures: bestmove per tier, depth correct-move first appears, all PV lines,
    and any Redux diagnostic strings (SearchDiag, AspirationEvent, StaticEvalInfo, etc.)

Also categorises each position using simple board heuristics (passed pawns, pins,
open files, king exposure, material imbalance, etc.). These categories feed directly
into stage 3's parameter-mapping logic.

Usage:
    python tools/game_diagnose.py --input results/analysis/2026-03_positions.json

    # Limit to first 20 positions (quick smoke test)
    python tools/game_diagnose.py --input results/analysis/2026-03_positions.json --max 20

    # Skip SF comparison (faster)
    python tools/game_diagnose.py --input results/analysis/2026-03_positions.json --no-sf

    # Adjust tiers (ms)
    python tools/game_diagnose.py --input ... --tiers 500 2000 8000

Output:
    results/analysis/<tag>_diagnosed.json
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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.dirname(os.path.dirname(_TOOLS_DIR))
_OUT_DIR   = os.path.join(_ROOT, "results", "analysis")

# Add tools/ to path for engine_config
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

try:
    from engine_config import find_engine
except ImportError:
    sys.exit("engine_config.py not found in tools/ â€” check your working directory")

try:
    import chess
    import chess.pgn
except ImportError:
    sys.exit("chess library required: pip install chess")


# ---------------------------------------------------------------------------
# Engine path discovery
# ---------------------------------------------------------------------------

def _find_sf() -> Optional[str]:
    """Locate the bundled Stockfish binary."""
    candidates = [
        os.path.join(_ROOT, "engines", "stockfish-17.1", "stockfish",
                     "stockfish-windows-x86-64-avx2.exe"),
        os.path.join(_ROOT, "engines", "stockfish-17.1", "stockfish",
                     "stockfish-ubuntu-x86-64-avx2"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Walk engines/ just in case
    engines_dir = os.path.join(_ROOT, "engines")
    if os.path.isdir(engines_dir):
        for root, _, files in os.walk(engines_dir):
            for f in files:
                if "stockfish" in f.lower() and not f.endswith((".json", ".md", ".txt")):
                    return os.path.join(root, f)
    return None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PVTier:
    movetime_ms:  int
    bestmove:     Optional[str]
    score_cp:     Optional[int]
    score_mate:   Optional[int]
    depth:        int
    nodes:        int
    nps:          int
    pv_uci:       str
    # All depth lines captured during this tier
    depth_lines:  list[dict] = field(default_factory=list)
    # Redux diagnostic strings (from info string ...)
    diagnostics:  list[str]  = field(default_factory=list)


@dataclass
class DiagnosedPosition:
    # From ingest
    game_id:       str
    ply:           int
    fen:           str
    our_move:      str
    stored_eval:   Optional[float]
    swing_cp:      Optional[float]
    phase:         str
    material:      int
    result:        str
    bot_color:     str

    # Probe results
    bot_tiers:     list[PVTier] = field(default_factory=list)
    sf_tier:       Optional[PVTier] = None

    # Derived
    sf_bestmove:   Optional[str]  = None
    bot_bestmove:  Optional[str]  = None   # from longest-time tier
    agrees_with_sf: bool          = False  # bot_bestmove == sf_bestmove
    first_sf_depth: Optional[int] = None   # depth our engine first matches SF's move
    categories:    list[str]      = field(default_factory=list)

    # Window / sequence metadata (pass-through from ingest)
    window_role:   str = "anchor"  # "anchor" or "context"
    anchor_ply:    int = 0         # ply of the anchor this position belongs to


# ---------------------------------------------------------------------------
# Position categorisation (purely board-based heuristics)
# ---------------------------------------------------------------------------

def _categorise(fen: str, bot_color: str) -> list[str]:
    """
    Return a list of category strings describing the position's major features.
    These map directly to the parameter groups in genetic_tune.py.
    """
    try:
        board = chess.Board(fen)
    except Exception:
        return ["unknown"]

    cats = []
    us   = chess.WHITE if bot_color == "white" else chess.BLACK
    them = not us

    # --- Passed pawns ---
    our_pawns   = board.pieces(chess.PAWN, us)
    their_pawns = board.pieces(chess.PAWN, them)
    passed = 0
    for sq in our_pawns:
        file_ = chess.square_file(sq)
        rank  = chess.square_rank(sq)
        ahead = chess.BB_FILES[file_]
        # All squares ahead on file + adjacent files
        if us == chess.WHITE:
            ahead_ranks = chess.BB_RANKS[rank] - 1  # ranks above
            ahead_mask  = sum(chess.BB_FILES[f] for f in
                              [file_-1, file_, file_+1] if 0 <= f <= 7) & ~((1 << (8*(rank+1))) - 1)
        else:
            ahead_mask  = sum(chess.BB_FILES[f] for f in
                              [file_-1, file_, file_+1] if 0 <= f <= 7) & ((1 << (8*rank)) - 1)
        if not (their_pawns & ahead_mask):
            passed += 1
    if passed >= 1:
        cats.append("passed_pawns")

    # --- Isolated / doubled pawns ---
    doubled = 0
    isolated = 0
    files_with_pawns = [chess.square_file(sq) for sq in our_pawns]
    for f in range(8):
        cnt = files_with_pawns.count(f)
        if cnt > 1:
            doubled += cnt - 1
        if cnt > 0:
            adj = [f-1, f+1]
            if not any(a in files_with_pawns for a in adj if 0 <= a <= 7):
                isolated += 1
    if doubled >= 1:
        cats.append("pawn_structure")
    if isolated >= 2:
        if "pawn_structure" not in cats:
            cats.append("pawn_structure")

    # --- Open / semi-open files with rooks ---
    our_rooks = board.pieces(chess.ROOK, us)
    for sq in our_rooks:
        f = chess.square_file(sq)
        all_pawns_on_file = (board.pieces(chess.PAWN, us)   | 
                             board.pieces(chess.PAWN, them)) & chess.BB_FILES[f]
        if not all_pawns_on_file:
            cats.append("open_files")
            break
        if not (board.pieces(chess.PAWN, us) & chess.BB_FILES[f]):
            cats.append("open_files")
            break

    # --- Knight outposts (knight on 4th/5th/6th rank not attackable by pawns) ---
    our_knights = board.pieces(chess.KNIGHT, us)
    for sq in our_knights:
        rank = chess.square_rank(sq)
        good_rank = rank >= 4 if us == chess.WHITE else rank <= 3
        if good_rank:
            # Check if enemy pawns can attack this square
            pawn_attackers = board.attackers(them, sq) & board.pieces(chess.PAWN, them)
            if not pawn_attackers:
                cats.append("knight_outposts")
                break

    # --- King safety ---
    our_king   = board.king(us)
    their_king = board.king(them)
    if our_king is not None:
        king_file = chess.square_file(our_king)
        king_rank = chess.square_rank(our_king)
        # Count attackers near our king
        attacker_count = 0
        for sq in chess.SquareSet(chess.BB_KING_ATTACKS[our_king]):
            if board.is_attacked_by(them, sq):
                attacker_count += 1
        if attacker_count >= 3:
            cats.append("king_safety")
        # King on open or semi-open file
        pawns_on_king_file = board.pieces(chess.PAWN, us) & chess.BB_FILES[king_file]
        if not pawns_on_king_file:
            cats.append("king_safety")

    # --- Pins (our pieces pinned to our king) ---
    if our_king is not None:
        pins = 0
        for sq in board.pieces(chess.BISHOP, us) | board.pieces(chess.ROOK, us) | \
                  board.pieces(chess.QUEEN, us)  | board.pieces(chess.KNIGHT, us):
            if board.is_pinned(us, sq):
                pins += 1
        if pins >= 1:
            cats.append("pins")

    # --- Material imbalance (bishop pair, minor piece imbalances) ---
    our_bishops   = len(board.pieces(chess.BISHOP, us))
    their_bishops = len(board.pieces(chess.BISHOP, them))
    our_knights_n = len(board.pieces(chess.KNIGHT, us))
    if our_bishops == 2 and their_bishops < 2:
        cats.append("bishop_pair")
    if our_bishops == 1 and our_knights_n == 0 and their_bishops == 0:
        cats.append("piece_activity")

    # --- Mobility (rough count of legal moves) ---
    try:
        our_moves  = board.legal_moves.count()
        board.push(chess.Move.null())  # swap turn
        their_moves = board.legal_moves.count() if not board.is_game_over() else 0
        board.pop()
        if our_moves < their_moves - 8:
            cats.append("mobility")
    except Exception:
        pass

    # --- Tactics indicators (high material on board = more tactical) ---
    queens = len(board.pieces(chess.QUEEN, us)) + len(board.pieces(chess.QUEEN, them))
    if queens >= 2:
        cats.append("tactical")

    # Default bucket
    if not cats:
        cats.append("positional")

    return cats


# ---------------------------------------------------------------------------
# UCI subprocess helpers
# ---------------------------------------------------------------------------

_INFO_RE  = re.compile(
    r'info\s+depth\s+(\d+)'
    r'(?:.*?seldepth\s+(\d+))?'
    r'(?:.*?score\s+(cp|mate)\s+([-\d]+))?'
    r'(?:.*?nodes\s+(\d+))?'
    r'(?:.*?nps\s+(\d+))?'
    r'(?:.*?pv\s+(.+))?'
)

def _run_engine_tier(
    engine_path: str,
    fen: str,
    movetime_ms: int,
    extra_options: dict,
) -> PVTier:
    """
    Start a UCI engine, send a position, run go movetime, collect all output.
    Returns a PVTier with the final bestmove and a list of all depth lines.
    """
    env = os.environ.copy()
    for p in [r"C:\mingw64\bin"]:
        if os.path.isdir(p) and p not in env.get("PATH", ""):
            env["PATH"] = p + os.pathsep + env.get("PATH", "")

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    proc = subprocess.Popen(
        [engine_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
        creationflags=flags,
    )

    lines_collected: list[str] = []
    stop_event = threading.Event()

    def _reader():
        for line in proc.stdout:
            lines_collected.append(line.rstrip())
            if "bestmove" in line:
                stop_event.set()

    reader_t = threading.Thread(target=_reader, daemon=True)
    reader_t.start()

    def _send(msg: str):
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()

    # Initialise
    _send("uci")
    # Wait for uciok
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("uciok" in l for l in lines_collected):
            break
        time.sleep(0.05)

    # Set options
    for name, value in extra_options.items():
        _send(f"setoption name {name} value {value}")
    _send("isready")
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("readyok" in l for l in lines_collected):
            break
        time.sleep(0.05)

    _send("ucinewgame")
    _send(f"position fen {fen}")
    _send(f"go movetime {movetime_ms}")

    stop_event.wait(timeout=movetime_ms / 1000 + 5)
    _send("quit")
    reader_t.join(timeout=2)

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

    # Parse collected lines
    tier = PVTier(
        movetime_ms=movetime_ms,
        bestmove=None,
        score_cp=None,
        score_mate=None,
        depth=0,
        nodes=0,
        nps=0,
        pv_uci="",
    )
    best_depth = -1

    for l in lines_collected:
        if l.startswith("bestmove"):
            parts = l.split()
            tier.bestmove = parts[1] if len(parts) > 1 else None

        elif l.startswith("info") and "depth" in l:
            if "info string" in l:
                tier.diagnostics.append(l)
                continue
            m = _INFO_RE.match(l)
            if not m:
                continue
            depth    = int(m.group(1))
            seldepth = int(m.group(2) or 0)
            kind     = m.group(3)
            val      = int(m.group(4)) if m.group(4) else None
            nodes    = int(m.group(5) or 0)
            nps      = int(m.group(6) or 0)
            pv       = (m.group(7) or "").strip()

            row = {
                "depth": depth, "seldepth": seldepth,
                "kind": kind, "val": val,
                "nodes": nodes, "nps": nps, "pv": pv,
            }
            tier.depth_lines.append(row)

            if depth > best_depth:
                best_depth       = depth
                tier.depth       = depth
                tier.nodes       = nodes
                tier.nps         = nps
                tier.pv_uci      = pv
                if kind == "cp":
                    tier.score_cp   = val
                    tier.score_mate = None
                elif kind == "mate":
                    tier.score_mate = val
                    tier.score_cp   = None

    return tier


# ---------------------------------------------------------------------------
# Diagnose one position
# ---------------------------------------------------------------------------

def diagnose_position(
    pos: dict,
    tiers_ms: list[int],
    bot_engine_path: str,
    sf_path: Optional[str],
    bot_options: dict,
    run_sf: bool = True,
) -> DiagnosedPosition:
    fen       = pos["fen_before"]
    our_move  = pos["our_move_uci"]
    bot_color = pos["bot_color"]

    diag = DiagnosedPosition(
        game_id     = pos["game_id"],
        ply         = pos["ply"],
        fen         = fen,
        our_move    = our_move,
        stored_eval = pos.get("stored_eval"),
        swing_cp    = pos.get("swing_cp"),
        phase       = pos.get("phase", "?"),
        material    = pos.get("material", 0),
        result      = pos.get("result", "*"),
        bot_color   = bot_color,
        categories  = _categorise(fen, bot_color),
        window_role = pos.get("window_role", "anchor"),
        anchor_ply  = pos.get("anchor_ply", 0),
    )

    # Run bot at each tier
    for ms in tiers_ms:
        tier = _run_engine_tier(bot_engine_path, fen, ms, bot_options)
        diag.bot_tiers.append(tier)

    # Best tier = longest
    if diag.bot_tiers:
        diag.bot_bestmove = diag.bot_tiers[-1].bestmove

    # Run SF
    if run_sf and sf_path:
        sf_tier = _run_engine_tier(sf_path, fen, max(tiers_ms), {})
        diag.sf_tier      = sf_tier
        diag.sf_bestmove  = sf_tier.bestmove

    # Find at which depth our engine first matches SF's bestmove
    if diag.sf_bestmove:
        diag.agrees_with_sf = (diag.bot_bestmove == diag.sf_bestmove)
        for tier in diag.bot_tiers:
            if tier.pv_uci.split()[0:1] == [diag.sf_bestmove]:
                diag.first_sf_depth = tier.depth
                break
            for row in tier.depth_lines:
                if row.get("pv", "").split()[0:1] == [diag.sf_bestmove]:
                    if diag.first_sf_depth is None or row["depth"] < diag.first_sf_depth:
                        diag.first_sf_depth = row["depth"]

    return diag


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deep-probe flagged positions from game_ingest.py output."
    )
    p.add_argument("--input",    required=True, metavar="JSON",
                   help="Path to *_positions.json from game_ingest.py")
    p.add_argument("--max",      type=int, default=None,
                   help="Max positions to diagnose (for quick test runs)")
    p.add_argument("--tiers",    type=int, nargs="+", default=[500, 2000, 5000],
                   metavar="MS",
                   help="Engine time tiers in ms (default: 500 2000 5000)")
    p.add_argument("--no-sf",    action="store_true",
                   help="Skip Stockfish comparison")
    p.add_argument("--engine",   metavar="PATH",
                   help="Override bot engine path")
    p.add_argument("--threads",  type=int, default=1)
    p.add_argument("--hash",     type=int, default=64,
                   help="Hash MB for engine (default: 64)")
    p.add_argument("--out-dir",  default=_OUT_DIR)
    p.add_argument("--out-tag",  default=None,
                   metavar="SUFFIX",
                   help="Append suffix to output filename, e.g. 'candidates' â†’ "
                        "<tag>_candidates_diagnosed.json.  Avoids overwriting the baseline.")
    p.add_argument("--params-json", default=None, metavar="PATH",
                   help="Path to *_param_candidates.json.  Applies suggested_value "
                        "for every listed parameter via setoption before each search.")
    p.add_argument("--quiet",    action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # Load positions
    with open(args.input) as f:
        data = json.load(f)

    positions = data.get("positions", [])
    if args.max:
        positions = positions[:args.max]

    tag = data.get("tag", "analysis")
    print(f"[diagnose] {len(positions)} positions to probe  |  "
          f"tiers: {args.tiers} ms  |  SF: {'no' if args.no_sf else 'yes'}")

    # Engine paths
    bot_path = args.engine or find_engine()
    if not bot_path or not os.path.isfile(bot_path):
        sys.exit(f"[diagnose] Bot engine not found: {bot_path}")

    sf_path = None if args.no_sf else _find_sf()
    if not args.no_sf and not sf_path:
        print("[diagnose] WARNING: Stockfish not found â€” disabling SF comparison")

    bot_options = {"Threads": args.threads, "Hash": args.hash,
                   "UseNNUE": "false"}   # HCE mode â€” EvalParams are effective here

    # Optional param overrides from a *_param_candidates.json
    if args.params_json:
        with open(args.params_json) as _pf:
            _pdata = json.load(_pf)
        _overrides = {c["name"]: c["suggested_value"] for c in _pdata.get("candidates", [])}
        bot_options.update(_overrides)
        print(f"[diagnose] Loaded {len(_overrides)} param overrides from {args.params_json}")

    results: list[dict] = []
    t0 = time.time()

    for i, pos in enumerate(positions):
        label = f"{pos['game_id']} ply={pos['ply']}"
        if not args.quiet:
            pct = (i+1) / len(positions) * 100
            elapsed = time.time() - t0
            eta = (elapsed / (i+1)) * (len(positions) - i - 1) if i else 0
            print(f"  [{pct:5.1f}%] {label:40s}  eta {eta/60:.1f}m", flush=True)

        try:
            diag = diagnose_position(
                pos,
                tiers_ms      = args.tiers,
                bot_engine_path = bot_path,
                sf_path         = sf_path,
                bot_options     = bot_options,
                run_sf          = not args.no_sf and sf_path is not None,
            )
            row = asdict(diag)
        except Exception as e:
            row = {"error": str(e), "game_id": pos["game_id"], "ply": pos["ply"]}
            if not args.quiet:
                print(f"     ERROR: {e}")

        results.append(row)

    # Save
    _out_name = f"{tag}_{args.out_tag}_diagnosed.json" if args.out_tag else f"{tag}_diagnosed.json"
    out_path = os.path.join(args.out_dir, _out_name)
    out_data = {
        "tag":        tag,
        "tiers_ms":   args.tiers,
        "with_sf":    not args.no_sf,
        "generated":  datetime.utcnow().isoformat(),
        "total":      len(results),
        "results":    results,
    }
    with open(out_path, "w") as f:
        json.dump(out_data, f, indent=2)

    ok    = sum(1 for r in results if "error" not in r)
    wrong = sum(1 for r in results if not r.get("agrees_with_sf"))
    print(f"\n[diagnose] done â€” {ok}/{len(results)} probed OK  |  "
          f"{wrong} disagree with SF  â†’  {out_path}")
    print(f"Next step: python tools/game_report.py --input {out_path}")


if __name__ == "__main__":
    main()
