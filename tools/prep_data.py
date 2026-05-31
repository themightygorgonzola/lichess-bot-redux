"""
prep_data.py — Convert training data to fast binary NNUE format.

One-time preprocessing step. Once done, training epochs load in milliseconds
via memory-map instead of re-parsing FENs every run.

Supports two input modes:
  --csv  : (fen, score_cp [, wdl]) CSV — e.g. train_1m.csv
  --pgn  : Annotated Lichess PGN (.pgn / .pgn.zst) with %eval comments
           Positions labeled directly from the embedded SF annotations —
           no need to run Stockfish again.

Output: .bin file (136-byte fixed records, numpy memmap compatible)

Usage:
  # Convert existing CSV (fastest — ~30s for 1M positions):
  python tools/prep_data.py --csv data/training/train_1m.csv --output data/training/train_1m.bin

  # Extract from Lichess PGN with embedded SF evals:
  python tools/prep_data.py --pgn data/pgn/lichess_db_standard_rated_2026-01-.pgn \\
      --output data/training/lichess_2026_01.bin --max-positions 20000000

  # Quick test subset:
  python tools/prep_data.py --csv data/training/train_1m.csv --output data/training/train_100k.bin \\
      --max-positions 100000
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# ── Project root ──────────────────────────────────────────────────────────
ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ml.data import BinaryWriter, RECORD_DTYPE, MAX_FEATS

# ── Position quality constants ────────────────────────────────────────────

# Scores beyond this are mate/forced-win annotations — not meaningful as
# centipawns.  Cap them so a single mate score doesn't dominate MSE loss.
# 3000cp ≈ sigmoid(3000/600) = 0.9933  ("clearly winning", not "infinite").
# Both CSV mate labels (±30000) and PGN mate annotations (±3000) collapse here.
DEFAULT_SCORE_CAP = 3000

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# Seconds between progress prints when tqdm is not available
_PRINT_INTERVAL = 5.0


# ── WDL synthesis from centipawns ─────────────────────────────────────────

def _cp_to_wdl(score_cp: float, scale: float = 600.0) -> float:
    """Convert centipawns (white's perspective) to WDL probability [0,1]."""
    return 1.0 / (1.0 + math.exp(-score_cp / scale))


# ── Worker function (module-level for pickling on Windows) ─────────────────

def _process_chunk(args: tuple) -> bytes:
    """
    Worker: list of (fen, score_cp, wdl_float) → serialised RECORD_DTYPE bytes.

    score_cp is from WHITE's perspective.
    wdl is already a float in [0,1] from WHITE's perspective.
    score_cap: scores are clamped to [-score_cap, +score_cap] and WDL
               re-derived from the clamped value for consistency.
    """
    root, positions, score_cap = args
    if root not in sys.path:
        sys.path.insert(0, root)

    import chess
    import numpy as np
    from ml.features import board_features
    from ml.arch import piece_count_bucket

    buf = np.zeros(len(positions), dtype=RECORD_DTYPE)
    good = 0

    for fen, score_cp, wdl in positions:
        try:
            board = chess.Board(fen)

            # Drop positions in check — eval less stable
            if board.is_check():
                continue

            wf, bf, pc = board_features(board)
            if pc < 3:
                continue

            n_w = min(len(wf), MAX_FEATS)
            n_b = min(len(bf), MAX_FEATS)

            # Cap score and re-derive WDL for consistency.
            # Mate labels (e.g. ±30000 from SF) become ±score_cap.
            # WDL is always derived from the capped score so the two
            # training targets agree with each other.
            if score_cap > 0:
                score_cp = float(np.clip(score_cp, -score_cap, score_cap))
                wdl = _cp_to_wdl(score_cp)

            r = buf[good]
            r['score']       = np.int16(int(score_cp))
            r['wdl']         = np.float16(float(wdl))
            r['stm']         = 1 if board.turn == chess.BLACK else 0
            r['bucket']      = piece_count_bucket(pc)
            r['n_white']     = n_w
            r['n_black']     = n_b
            r['white_feats'][:n_w] = wf[:n_w]
            r['black_feats'][:n_b] = bf[:n_b]
            good += 1
        except Exception:
            pass

    return buf[:good].tobytes()


# ── CSV source ────────────────────────────────────────────────────────────

def _iter_csv(path: str):
    """
    Yield (fen, score_cp, wdl) tuples from a CSV file.

    Supports columns:
      fen, score / score_cp [, wdl]
    Score is assumed to be from WHITE's perspective.
    WDL synthesised from score if not present.
    """
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return

        hl = [h.strip().lower() for h in header]
        col_fen   = hl.index('fen') if 'fen' in hl else 0
        col_score = (
            hl.index('score_cp') if 'score_cp' in hl else
            hl.index('score')    if 'score'    in hl else 1
        )
        col_wdl = hl.index('wdl') if 'wdl' in hl else -1

        for row in reader:
            try:
                fen   = row[col_fen].strip()
                score = float(row[col_score])
                wdl   = float(row[col_wdl]) if col_wdl >= 0 else _cp_to_wdl(score)
                yield fen, score, wdl
            except (IndexError, ValueError):
                pass


# ── PGN source ─────────────────────────────────────────────────────────────

_EVAL_RE  = re.compile(r'\[%eval\s+([+\-]?\d+\.?\d*|#[+\-]?\d+)\]')
_CLOCK_RE = re.compile(r'\[%clk\s+\S+\]')

_TC_RE = re.compile(r'^(\d+)\+')

def _iter_pgn(path: str, min_elo: int = 0, skip_plies: int = 10,
              sample_every: int = 3, skip_captures: bool = True,
              min_time_control: int = 0):
    """
    Yield (fen, score_cp, wdl) from a Lichess-annotated PGN.

    Lichess monthly dumps contain Stockfish %eval annotations in move comments.
    This extracts positions + evals without running SF, using the annotations
    already present in the file.

    skip_captures: if True, skip positions reached by a capture move.
      After a capture the position may be mid-exchange and the eval swings
      dramatically ply-to-ply.  Stockfish qsearch stabilises this, but
      excluding them produces cleaner training signal.

    min_time_control: minimum base-time in seconds (e.g. 180 = blitz+ only).
      Bullet games tend to have more tactical noise and premove sequences.
      The SF eval is computed post-game at fixed depth regardless, but
      positions from thoughtful games are higher quality training signal.
      0 = no filter (all time controls).

    Handles both plain .pgn and .pgn.zst (compressed) files.
    """
    import chess.pgn
    import io

    # Handle optional zstd compression
    if path.endswith('.zst'):
        try:
            import zstandard as zstd
        except ImportError:
            raise ImportError("pip install zstandard  (needed for .zst files)")
        dctx = zstd.ZstdDecompressor()
        raw = open(path, 'rb')
        stream = io.TextIOWrapper(dctx.stream_reader(raw), encoding='utf-8')
    else:
        stream = open(path, 'r', encoding='utf-8', errors='replace')

    games_read = 0
    positions_yielded = 0
    t0 = time.time()
    t_last_print = t0

    try:
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            games_read += 1

            # Heartbeat — print every 5s even before any positions are written
            now = time.time()
            if now - t_last_print >= 5.0:
                elapsed = now - t0
                rate_g = games_read / elapsed
                rate_p = positions_yielded / elapsed
                print(f"  [pgn] {elapsed:6.0f}s  games={games_read:,}  pos_found={positions_yielded:,}"
                      f"  ({rate_g:,.0f} games/s  {rate_p:,.0f} pos/s)", flush=True)
                t_last_print = now

            # ELO filter (both players must meet threshold)
            if min_elo > 0:
                try:
                    w_elo = int(game.headers.get('WhiteElo', 0))
                    b_elo = int(game.headers.get('BlackElo', 0))
                    if min_elo > min(w_elo, b_elo):
                        continue
                except ValueError:
                    continue

            # Time control filter (skip bullet / ultra-bullet)
            if min_time_control > 0:
                tc_str = game.headers.get('TimeControl', '')
                m_tc = _TC_RE.match(tc_str)
                if not m_tc or int(m_tc.group(1)) < min_time_control:
                    continue

            board = game.board()
            ply = 0
            ply_offset = random.randrange(sample_every) if sample_every > 1 else 0
            node = game.next()

            while node is not None:
                # Check capture BEFORE push — board.is_capture() queries the
                # current board state (what's at the destination square).
                # After push the captured piece is already gone.
                is_cap = skip_captures and board.is_capture(node.move)

                board.push(node.move)
                ply += 1

                if ply < skip_plies or ((ply - ply_offset) % sample_every) != 0 or is_cap:
                    node = node.next()
                    continue

                comment = node.comment
                m = _EVAL_RE.search(comment)
                if m:
                    eval_str = m.group(1)
                    if eval_str.startswith('#'):
                        # Mate annotation: use ±3000 cp
                        score_cp = -3000.0 if '-' in eval_str else 3000.0
                    else:
                        score_cp = float(eval_str) * 100.0  # pawns → cp

                    # Annotations are from the side that JUST MOVED
                    # After pushing node.move, board.turn is the NEXT player.
                    # The eval is from the perspective of the player to move
                    # AFTER the move (i.e. the opponent of who just moved).
                    # Standardise to white's perspective:
                    if board.turn == chess.BLACK:
                        # The current player-to-move is black → eval is black's view
                        # Negate to get white's view
                        score_cp = -score_cp

                    wdl = _cp_to_wdl(score_cp)
                    fen = board.fen()
                    yield fen, score_cp, wdl
                    positions_yielded += 1

                node = node.next()

    finally:
        stream.close()


# ── Core conversion pipeline ──────────────────────────────────────────────

def convert(
    source,              # iterable of (fen, score_cp, wdl)
    output_path: str,
    workers: int,
    chunk_size: int,
    max_positions: int,
    label: str = "Converting",
    score_cap: int = DEFAULT_SCORE_CAP,
) -> int:
    """
    Run fen+score → binary conversion with a process pool.

    Returns total records written.
    """
    total_written = 0
    t0 = time.time()
    last_print = t0

    chunk: list = []
    futures = []

    bar = None
    if HAS_TQDM and max_positions > 0:
        bar = tqdm(total=max_positions, unit='pos', desc=label, dynamic_ncols=True)

    with BinaryWriter(output_path) as writer:
        with ProcessPoolExecutor(max_workers=workers) as pool:

            def flush_chunk():
                nonlocal chunk
                if chunk:
                    fut = pool.submit(_process_chunk, (ROOT, chunk, score_cap))
                    futures.append(fut)
                    chunk = []

            def drain_completed():
                nonlocal total_written
                done = [f for f in futures if f.done()]
                for f in done:
                    raw = f.result()
                    arr = np.frombuffer(raw, dtype=RECORD_DTYPE)
                    writer.write_batch(arr)
                    total_written += len(arr)
                    futures.remove(f)
                    if bar:
                        bar.update(len(arr))
                    elif time.time() - last_print > _PRINT_INTERVAL:
                        _print_progress(label, total_written, t0)

            for fen, score_cp, wdl in source:
                chunk.append((fen, score_cp, wdl))

                if len(chunk) >= chunk_size:
                    flush_chunk()
                    # Keep the pool busy but avoid unbounded memory use
                    while len(futures) >= workers * 2:
                        drain_completed()
                        if len(futures) >= workers * 2:
                            futures[0].result()  # block on oldest

                if max_positions > 0 and total_written >= max_positions:
                    break

            # Flush remainder
            flush_chunk()
            for f in futures:
                raw = f.result()
                arr = np.frombuffer(raw, dtype=RECORD_DTYPE)
                writer.write_batch(arr)
                total_written += len(arr)
                if bar:
                    bar.update(len(arr))

    if bar:
        bar.close()

    elapsed = time.time() - t0
    rate = total_written / elapsed if elapsed > 0 else 0
    print(f"\n{label}: {total_written:,} records in {elapsed:.1f}s  ({rate:,.0f} pos/s)")
    file_mb = os.path.getsize(output_path) / 1024**2
    print(f"Output: {output_path}  ({file_mb:.1f} MB)")
    return total_written


import numpy as np  # needed at module level for drain_completed (called from pool callback)


# ── Parallel PGN splitter ─────────────────────────────────────────────────

def _find_split_points(path: str, n_splits: int) -> list:
    """
    Scan the PGN file in binary mode to find n_splits evenly-spaced game
    start offsets.  Searches for the byte pattern b'\n\n[Event ' near each
    target offset.  O(n_splits * 128KB) IO — fast even for 200GB files.

    Returns a list of n_splits+1 byte offsets: [0, ..., file_size].
    Each adjacent pair [splits[i], splits[i+1]) is one independent chunk.
    """
    file_size = os.path.getsize(path)
    if n_splits <= 1:
        return [0, file_size]

    marker = b'\n\n[Event '
    scan_window = 131072  # 128 KB — plenty to find a game boundary
    splits = [0]

    with open(path, 'rb') as f:
        for i in range(1, n_splits):
            target = i * file_size // n_splits
            seek_pos = max(0, target - 256)  # small back-up so marker isn't split
            f.seek(seek_pos)
            buf = f.read(scan_window)
            idx = buf.find(marker)
            if idx >= 0:
                splits.append(seek_pos + idx + 2)  # +2: skip the \n\n, land on '['
            else:
                splits.append(target)  # fallback — extremely rare

    splits.append(file_size)
    return sorted(set(splits))  # deduplicate (can happen near file start)


def _process_pgn_chunk(args: tuple):
    """
    Parallel PGN worker: opens the file independently, seeks to start_byte,
    reads games until raw file position passes end_byte OR max_out positions
    collected, applies all filters, computes HalfKAv2 features.
    Returns (n_games, n_pos, bytes).
    """
    (root, path, start_byte, end_byte,
     min_elo, min_time_control, skip_plies, sample_every,
        skip_captures, score_cap, result_wdl_blend, max_out) = args

    if root not in sys.path:
        sys.path.insert(0, root)

    import io as _io
    import math as _math
    import re as _re
    import numpy as np
    import chess
    import chess.pgn
    from ml.features import board_features
    from ml.arch import piece_count_bucket
    from ml.data import RECORD_DTYPE, MAX_FEATS

    EVAL_RE = _re.compile(r'\[%eval\s+([+\-]?\d+\.?\d*|#[+\-]?\d+)\]')
    TC_RE   = _re.compile(r'^(\d+)\+')

    def cp_to_wdl(score, scale=600.0):
        return 1.0 / (1.0 + _math.exp(-score / scale))

    out = np.zeros(max_out, dtype=RECORD_DTYPE)
    good = 0
    n_games = 0

    raw  = open(path, 'rb')
    raw.seek(start_byte)
    text = _io.TextIOWrapper(raw, encoding='utf-8', errors='replace')

    # Fast header extraction regexes — run on raw game text before chess.pgn parse
    ELO_RE    = _re.compile(r'\[(?:White|Black)Elo\s+"(\d+)"\]')
    TC_RAW_RE = _re.compile(r'\[TimeControl\s+"(\d+)\+')
    RES_RE    = _re.compile(r'\[Result\s+"([^"]+)"\]')

    def _handle_game(gtext):
        nonlocal good, n_games
        if not gtext:
            return
        n_games += 1

        # ---- Fast pre-filters on raw text (no chess.pgn overhead) ----

        # Skip games with no eval annotations (91% of games in Lichess dumps)
        if '%eval' not in gtext:
            return

        # ELO filter — regex on raw header text
        if min_elo > 0:
            elos = ELO_RE.findall(gtext)
            if len(elos) < 2 or min(int(e) for e in elos) < min_elo:
                return

        # Time control filter — regex on raw header text
        if min_time_control > 0:
            tc_m = TC_RAW_RE.search(gtext)
            if not tc_m or int(tc_m.group(1)) < min_time_control:
                return

        # Game result WDL for label blending (1.0=white win, 0.5=draw, 0.0=black win)
        res_m = RES_RE.search(gtext)
        result_str = res_m.group(1) if res_m else '*'
        result_wdl = {'1-0': 1.0, '0-1': 0.0, '1/2-1/2': 0.5}.get(result_str, 0.5)

        # ---- Full chess.pgn parse — only for qualifying games (~9%) ----
        game = chess.pgn.read_game(_io.StringIO(gtext))
        if game is None:
            return

        board = game.board()
        ply   = 0
        ply_offset = random.randrange(sample_every) if sample_every > 1 else 0
        node  = game.next()

        while node is not None:
            if good >= max_out:
                break
            is_cap = skip_captures and board.is_capture(node.move)
            board.push(node.move)
            ply += 1

            if ply < skip_plies or ((ply - ply_offset) % sample_every) != 0 or is_cap:
                node = node.next()
                continue

            comment = node.comment
            m = EVAL_RE.search(comment)
            if m:
                ev = m.group(1)
                score_cp = (-3000.0 if '-' in ev else 3000.0) if ev.startswith('#') \
                           else float(ev) * 100.0
                if board.turn == chess.BLACK:
                    score_cp = -score_cp
                if score_cap > 0:
                    score_cp = max(-float(score_cap), min(float(score_cap), score_cp))
                # Keep score and WDL targets consistent by default.
                # Optional result blending is available for experiments.
                wdl = ((1.0 - result_wdl_blend) * cp_to_wdl(score_cp)
                       + result_wdl_blend * result_wdl)

                try:
                    if board.is_check():
                        node = node.next()
                        continue
                    # Quiet filter: skip positions where captures are available
                    if any(board.is_capture(mv) for mv in board.legal_moves):
                        node = node.next()
                        continue
                    wf, bf, pc = board_features(board)
                    if pc < 3:
                        node = node.next()
                        continue
                    n_w = min(len(wf), MAX_FEATS)
                    n_b = min(len(bf), MAX_FEATS)
                    r = out[good]
                    r['score']       = np.int16(int(score_cp))
                    r['wdl']         = np.float16(float(wdl))
                    r['stm']         = 1 if board.turn == chess.BLACK else 0
                    r['bucket']      = piece_count_bucket(pc)
                    r['n_white']     = n_w
                    r['n_black']     = n_b
                    r['white_feats'][:n_w] = wf[:n_w]
                    r['black_feats'][:n_b] = bf[:n_b]
                    good += 1
                except Exception:
                    pass

            node = node.next()

    # ---- Line-buffer game collector ----
    # Iterate lines directly — O(bytes) overhead for the 91% skip case
    # instead of O(moves * parse) from chess.pgn.read_game on every game.
    try:
        buf = []
        for line in text:
            if line.startswith('[Event '):
                if buf:
                    _handle_game(''.join(buf))
                    if good >= max_out:
                        break
                    if raw.tell() >= end_byte:
                        break
                buf = [line]
            else:
                buf.append(line)
        # Handle last game in buffer
        if buf and good < max_out:
            _handle_game(''.join(buf))
    finally:
        text.detach()  # don't let TextIOWrapper close raw on GC
        raw.close()

    return n_games, good, out[:good].tobytes()


def convert_pgn_parallel(
    path: str,
    output_path: str,
    workers: int,
    max_positions: int,
    min_elo: int,
    min_time_control: int,
    skip_plies: int,
    sample_every: int,
    skip_captures: bool,
    score_cap: int,
    result_wdl_blend: float,
) -> int:
    """
    Fast parallel PGN extraction.

    Splits the file into n_chunks byte-range slices, dispatches workers
    with backpressure (only workers*2 futures in flight at once) so the
    result queue never grows large.  Each chunk has a per-chunk position
    budget so workers exit early once they've collected enough.
    """
    import numpy as np
    from concurrent.futures import wait, FIRST_COMPLETED
    from ml.data import RECORD_DTYPE

    n_splits = workers * 64
    t0 = time.time()

    print(f"Scanning file for {n_splits} split points...", flush=True)
    splits = _find_split_points(path, n_splits)
    n_chunks = len(splits) - 1
    print(f"  Found {n_chunks} chunks in {time.time()-t0:.1f}s", flush=True)

    # Per-chunk position budget:
    #   If max_positions set: budget = enough that workers finish fast,
    #   but with 3x headroom so overlap between chunks covers gaps in filter yield.
    #   If full scan: large budget so each chunk processes its full byte range.
    if max_positions > 0:
        per_chunk_budget = max(2000, (max_positions // n_chunks) * 4)
    else:
        per_chunk_budget = 500_000  # ~68MB per chunk result; generous for full scans

    total_written = 0
    total_games   = 0
    chunks_done   = 0
    chunk_idx     = 0
    t_last        = t0

    bar = tqdm(total=max_positions, unit='pos', desc='PGN->binary',
               dynamic_ncols=True) if HAS_TQDM and max_positions > 0 else None

    in_flight = {}   # fut -> chunk_index

    pool_broken = [False]

    def _submit_next():
        nonlocal chunk_idx
        from concurrent.futures.process import BrokenProcessPool as _BPP
        while chunk_idx < n_chunks and len(in_flight) < workers * 2:
            if max_positions > 0 and total_written >= max_positions:
                break
            args = (ROOT, path, splits[chunk_idx], splits[chunk_idx + 1],
                    min_elo, min_time_control, skip_plies,
                    sample_every, skip_captures, score_cap, result_wdl_blend,
                    per_chunk_budget)
            try:
                fut = pool.submit(_process_pgn_chunk, args)
            except _BPP as e:
                print(f"  [pool broken during submit] {e}", flush=True)
                pool_broken[0] = True
                return
            in_flight[fut] = chunk_idx
            chunk_idx += 1

    with BinaryWriter(output_path) as writer:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            _submit_next()  # fill initial slots

            while in_flight:
                if max_positions > 0 and total_written >= max_positions:
                    for f in list(in_flight):
                        if f.cancel():
                            del in_flight[f]
                    if not in_flight:
                        break

                # 5s timeout so the loop always wakes up for the heartbeat
                done, _ = wait(list(in_flight.keys()),
                               return_when=FIRST_COMPLETED, timeout=5.0)

                # Heartbeat — fires every 5s even when no chunk has finished
                now = time.time()
                elapsed = now - t0
                if elapsed > 0 and now - t_last >= _PRINT_INTERVAL:
                    eta_s = ''
                    if max_positions > 0 and total_written > 0:
                        rate_now = total_written / elapsed
                        remaining_pos = max_positions - total_written
                        eta_s = f'  ETA={remaining_pos/rate_now/60:.1f}min'
                    elif chunks_done > 0:
                        # Unlimited run: estimate from chunk completion rate
                        chunk_rate = chunks_done / elapsed          # chunks/s
                        remaining_chunks = n_chunks - chunks_done
                        eta_min = remaining_chunks / chunk_rate / 60
                        pct = 100.0 * chunks_done / n_chunks
                        pos_rate = total_written / elapsed
                        est_total = int(total_written * n_chunks / chunks_done)
                        eta_s = f'  {pct:.1f}%  ETA={eta_min:.0f}min  est_total={est_total:,}'
                    print(f"  [pgn] {elapsed:6.0f}s  chunks={chunks_done}/{n_chunks}"
                          f"  in_flight={len(in_flight)}  games={total_games:,}"
                          f"  pos={total_written:,}"
                          f"  ({total_written/elapsed:,.0f} pos/s){eta_s}", flush=True)
                    t_last = now

                if pool_broken[0]:
                    in_flight.clear()
                    break

                for fut in done:
                    del in_flight[fut]
                    try:
                        n_games, n_pos, raw_bytes = fut.result()
                    except Exception as e:
                        from concurrent.futures.process import BrokenProcessPool as _BPP
                        import traceback as _tb
                        print(f"  [chunk error] {e}\n{_tb.format_exc()}", flush=True)
                        if isinstance(e, _BPP):
                            pool_broken[0] = True
                            in_flight.clear()
                            break
                        continue

                    if raw_bytes:
                        arr = np.frombuffer(raw_bytes, dtype=RECORD_DTYPE)
                        if max_positions > 0:
                            remaining = max_positions - total_written
                            if len(arr) > remaining:
                                arr = arr[:remaining]
                        writer.write_batch(arr)
                        total_written += len(arr)
                        if bar:
                            bar.update(len(arr))

                    total_games += n_games
                    chunks_done += 1

                _submit_next()  # refill slots

    if bar:
        bar.close()

    elapsed = time.time() - t0
    rate = total_written / elapsed if elapsed > 0 else 0
    print(f"\nPGN->binary: {total_written:,} records in {elapsed:.1f}s  ({rate:,.0f} pos/s)")
    file_mb = os.path.getsize(output_path) / 1024**2
    print(f"Output: {output_path}  ({file_mb:.1f} MB)")
    return total_written


def _print_progress(label, n, t0):
    elapsed = time.time() - t0
    rate = n / elapsed if elapsed > 0 else 0
    print(f"  {label}: {n:,} written  {elapsed:.0f}s  {rate:,.0f} pos/s", flush=True)


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import multiprocessing
    cpu_count = multiprocessing.cpu_count()

    parser = argparse.ArgumentParser(
        description="Convert training data to fast binary NNUE format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input (mutually exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--csv', metavar='FILE',
                       help='Input CSV (columns: fen, score/score_cp [, wdl])')
    group.add_argument('--pgn', metavar='FILE',
                       help='Input annotated PGN (.pgn or .pgn.zst)')

    parser.add_argument('--output', required=True, metavar='FILE',
                        help='Output .bin file path')
    parser.add_argument('--workers', type=int, default=max(1, cpu_count // 2),
                        metavar='N',
                        help=f'Worker processes (default: {max(1, cpu_count // 2)})')
    parser.add_argument('--chunk-size', type=int, default=10_000, metavar='N',
                        help='Positions per work chunk (default: 10000)')
    parser.add_argument('--max-positions', type=int, default=0, metavar='N',
                        help='Stop after N positions (0 = all)')

    # PGN-only options
    parser.add_argument('--min-elo', type=int, default=1800, metavar='N',
                        help='[PGN] Minimum ELO for BOTH players (default: 1800)')
    parser.add_argument('--skip-plies', type=int, default=10, metavar='N',
                        help='[PGN] Skip first N half-moves per game — reduces opening repetition (default: 10)')
    parser.add_argument('--sample-every', type=int, default=3, metavar='N',
                        help='[PGN] Keep 1 in N positions to reduce correlation (default: 3)')
    parser.add_argument('--no-skip-captures', action='store_true',
                        help='[PGN] Include positions reached by capture moves (default: skip them)')
    parser.add_argument('--min-time-control', type=int, default=180, metavar='SECS',
                        help='[PGN] Minimum base-time in seconds (default: 180 = blitz+, skips bullet). Use 0 for all.')

    # Score quality
    parser.add_argument('--score-cap', type=int, default=DEFAULT_SCORE_CAP, metavar='CP',
                        help=f'Cap scores to ±N cp and re-derive WDL (default: {DEFAULT_SCORE_CAP}). '
                              'Mate labels (±30000 from SF) become ±score-cap. Use 0 to disable.')
    parser.add_argument('--result-wdl-blend', type=float, default=0.0, metavar='FRAC',
                        help='[PGN] Blend actual game result into stored WDL. 0.0 = disabled (default); 0.3 reproduces the previous behavior.')

    args = parser.parse_args()

    print(f"Workers:     {args.workers}")
    print(f"Chunk size:  {args.chunk_size:,}")
    print(f"Score cap:   {'disabled' if args.score_cap == 0 else f'±{args.score_cap} cp'}")
    print(f"Result WDL:  blend={args.result_wdl_blend:.2f}")
    print(f"Max output:  {'unlimited' if args.max_positions == 0 else f'{args.max_positions:,}'}")
    print(f"Output:      {args.output}")
    if args.pgn:
        tc_label = f">={args.min_time_control}s (blitz+)" if args.min_time_control == 180 else \
                   (f">={args.min_time_control}s" if args.min_time_control > 0 else "all (no filter)")
        print(f"Filters:     ELO>={args.min_elo}  TimeControl={tc_label}  "
              f"skip_captures={not args.no_skip_captures}  skip_plies={args.skip_plies}")
    print()

    if args.csv:
        print(f"Source: CSV  {args.csv}")
        source = _iter_csv(args.csv)
        label = "CSV->binary"
        n = convert(source, args.output, args.workers, args.chunk_size,
                    args.max_positions, label, score_cap=args.score_cap)
    else:
        skip_caps = not args.no_skip_captures
        tc_label = f">={args.min_time_control}s" if args.min_time_control > 0 else "all"
        print(f"Source: PGN  {args.pgn}")
        print(f"  min_elo={args.min_elo}  time_control={tc_label}  "
              f"skip_plies={args.skip_plies}  sample_every={args.sample_every}  "
              f"skip_captures={skip_caps}")
        if args.pgn.endswith('.zst'):
            # .pgn.zst can't be byte-range split while compressed, but we can
            # decompress to a temp file first and then use the full parallel path.
            # This is dramatically faster because chess.pgn parsing (not zstd
            # decompression) is the real bottleneck — decompression runs at
            # ~500 MB/s, while serial PGN parsing is ~800 games/s on one core.
            # Disk space warning: decompressed Lichess monthly dumps are 20-60 GB.
            import tempfile
            try:
                import zstandard as zstd
            except ImportError:
                raise ImportError("pip install zstandard  (needed for .zst files)")

            zst_size = os.path.getsize(args.pgn)
            est_decomp_gb = zst_size * 7 / 1024**3   # zst ratio ≈ 7x for PGN
            print(f"  (zst detected -- decompressing to temp file for full parallel processing)")
            print(f"  Compressed: {zst_size/1024**3:.1f} GB   Estimated decompressed: ~{est_decomp_gb:.0f} GB")
            print(f"  Make sure you have ~{est_decomp_gb*1.1:.0f} GB free on the output drive.", flush=True)

            tmp_dir = os.path.dirname(os.path.abspath(args.pgn))
            tmp_fd, tmp_path = tempfile.mkstemp(suffix='.pgn', dir=tmp_dir,
                                                 prefix='prep_data_decomp_')
            try:
                dctx = zstd.ZstdDecompressor()
                t_decomp = time.time()
                with open(args.pgn, 'rb') as f_in, os.fdopen(tmp_fd, 'wb') as f_out:
                    dctx.copy_stream(f_in, f_out, write_size=1024 * 1024)
                tmp_fd = -1  # fdopen owns it now; don't double-close
                decomp_size = os.path.getsize(tmp_path)
                print(f"  Decompressed {decomp_size/1024**3:.2f} GB in {time.time()-t_decomp:.1f}s  "
                      f"({decomp_size/1024**2/(time.time()-t_decomp):.0f} MB/s)", flush=True)

                n = convert_pgn_parallel(
                    path=tmp_path,
                    output_path=args.output,
                    workers=args.workers,
                    max_positions=args.max_positions,
                    min_elo=args.min_elo,
                    min_time_control=args.min_time_control,
                    skip_plies=args.skip_plies,
                    sample_every=args.sample_every,
                    skip_captures=skip_caps,
                    score_cap=args.score_cap,
                    result_wdl_blend=args.result_wdl_blend,
                )
            finally:
                if tmp_fd != -1:
                    try:
                        os.close(tmp_fd)
                    except OSError:
                        pass
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                    print("  Temp file removed.", flush=True)
        else:
            n = convert_pgn_parallel(
                path=args.pgn,
                output_path=args.output,
                workers=args.workers,
                max_positions=args.max_positions,
                min_elo=args.min_elo,
                min_time_control=args.min_time_control,
                skip_plies=args.skip_plies,
                sample_every=args.sample_every,
                skip_captures=skip_caps,
                score_cap=args.score_cap,
                result_wdl_blend=args.result_wdl_blend,
            )
        label = "PGN->binary"

    if n > 0:
        print(f"\nInspecting output:")
        from ml.data import inspect
        inspect(args.output)
    else:
        print("Warning: 0 records written — check input file format.")


if __name__ == '__main__':
    main()
