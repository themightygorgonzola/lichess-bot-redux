"""
selfplay_match.py â€” Generate NNUE training positions from engine self-play.

Runs N games between any two UCI engines (redux-hce.exe, Berserk, Obsidian,
Stormphrax, Clover, Stockfish, ...). Every quiet non-opening position from both
sides of every game is extracted, annotated with the playing engine's own search
score, and written as RECORD_DTYPE records directly into the training pipeline.

  â€¢ Colors are alternated each game (engine1 plays white in even games, black in odd)
    to guarantee balanced data regardless of engine strength differential.
  â€¢ Adjudication at Â±2000 cp Ã— 5 consecutive plies avoids unending resignations.
  â€¢ Output is a standard .bin compatible with mean-alltime-dedup-shuffled.bin â€” 
    append/merge with existing data before the next training run.

Usage:
    python tools/selfplay_match.py \\
        --engine1 engines/berserk/berserk.exe \\
        --engine2 engines/obsidian/obsidian.exe \\
        --games 2000 --nodes 20000 --workers 4 \\
        --output data/processed/selfplay_berserk_obsidian.bin

    python tools/selfplay_match.py \\
        --engine1 build/redux-hce.exe \\
        --engine2 engines/stormphrax/stormphrax.exe \\
        --games 1000 --nodes 30000 --workers 2 \\
        --output data/processed/selfplay_redux_stormphrax.bin

Merging into training data afterwards:
    # Concatenate all selfplay bins with the main shuffled dataset, then re-shuffle.
    python tools/concat_bins.py \\
        data/processed/mean-alltime-dedup-shuffled.bin \\
        data/processed/selfplay_*.bin \\
        --output data/processed/merged.bin
"""

from __future__ import annotations

import argparse
import math
import os
import random
import struct
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import chess
import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from ml.data import RECORD_DTYPE, RECORD_SIZE, HEADER_SIZE, HEADER_MAGIC, MAX_FEATS
from ml.features import board_features
from ml.arch import piece_count_bucket, INPUT_SIZE

WDL_SCALE        = 600.0
ADJUDICATION_CP  = 2000   # |eval| must exceed this cp...
ADJUDICATION_N   = 5      # ...for this many consecutive plies â†’ adjudicate
MAX_HALFMOVES    = 400     # hard game length cap (200 full moves)

# â”€â”€ Built-in opening book â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# FEN positions after 1-4 moves covering the main openings.
# Provides enough variety for useful self-play data without an external book.
BUILTIN_OPENINGS: list[str] = [
    # Startpos
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    # 1.e4
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
    "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
    "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
    # Sicilian
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
    "rnbqkb1r/pp1ppppp/5n2/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
    "r1bqkbnr/pp1ppppp/2n5/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
    # French
    "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    "rnbqkbnr/pppp1ppp/4p3/8/4P3/2N5/PPPP1PPP/R1BQKBNR b KQkq - 1 2",
    "rnbqkbnr/ppp2ppp/4p3/3p4/3PP3/2N5/PPP2PPP/R1BQKBNR w KQkq d6 0 3",
    # Caro-Kann
    "rnbqkbnr/pp1ppppp/2p5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    "rnbqkbnr/pp1ppppp/2p5/8/3PP3/8/PPP2PPP/RNBQKBNR b KQkq d3 0 2",
    "rnbqkbnr/pp1ppppp/2p5/8/4P3/2N5/PPPP1PPP/R1BQKBNR b KQkq - 1 2",
    # Pirc/Modern
    "rnbqkbnr/pppppp1p/6p1/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    "rnbqkbnr/pppppppp/8/8/4P3/2N5/PPPP1PPP/R1BQKBNR b KQkq - 1 2",
    # Scandinavian
    "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2",
    # 1.d4
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq d3 0 1",
    "rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq d6 0 2",
    "rnbqkbnr/ppp1pppp/8/3p4/2PP4/8/PP2PPPP/RNBQKBNR b KQkq c3 0 2",
    "rnbqkbnr/ppp1pppp/8/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR b KQkq - 1 3",
    # Queen's Gambit Declined
    "rnbqkbnr/ppp1pppp/8/3p4/2PP4/2N2N2/PP2PPPP/R1BQKB1R b KQkq - 2 4",
    # King's Indian
    "rnbqkb1r/pppppp1p/5np1/8/2PP4/8/PP2PPPP/RNBQKBNR w KQkq - 1 3",
    "rnbqkb1r/pppppp1p/5np1/8/2PPP3/8/PP3PPP/RNBQKBNR b KQkq e3 0 3",
    # Nimzo-Indian
    "rnbqk2r/pppp1ppp/4pn2/8/1bPP4/2N5/PP2PPPP/R1BQKBNR w KQkq - 2 4",
    # GrÃ¼nfeld
    "rnbqkb1r/ppp1pp1p/5np1/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq d6 0 4",
    # Dutch
    "rnbqkbnr/ppppp1pp/8/5p2/3P4/8/PPP1PPPP/RNBQKBNR w KQkq f6 0 2",
    # English
    "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq c3 0 1",
    "rnbqkb1r/pppppppp/5n2/8/2P5/2N5/PP1PPPPP/R1BQKBNR b KQkq - 2 2",
    "rnbqkb1r/pppp1ppp/4pn2/8/2P5/2N5/PP1PPPPP/R1BQKBNR w KQkq - 0 3",
    # Reti / 1.Nf3
    "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1",
    "rnbqkb1r/pppppppp/5n2/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 2 2",
    # London
    "rnbqkbnr/pppppppp/8/8/3P4/5N2/PPP1PPPP/RNBQKB1R b KQkq - 1 2",
    "rnbqkb1r/pppppppp/5n2/8/3P1B2/5N2/PPP1PPPP/RN1QKB1R b KQkq - 3 3",
    # Catalan
    "rnbqkb1r/ppp2ppp/4pn2/3p4/2PP4/5N2/PP2PPPP/RNBQKB1R w KQkq d6 0 4",
    # Bird's
    "rnbqkbnr/pppppppp/8/8/5P2/8/PPPPP1PP/RNBQKBNR b KQkq f3 0 1",
    # Colle
    "rnbqkb1r/ppp1pppp/5n2/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq - 2 3",
]


# â”€â”€ UCI Engine wrapper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class UCIEngine:
    """Thin UCI subprocess wrapper. Each instance owns one engine process."""

    def __init__(self, path: str, hash_mb: int = 32, threads: int = 1):
        self.path = path
        self.proc = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self._send('uci')
        self._wait_for('uciok')
        self._send(f'setoption name Hash value {hash_mb}')
        self._send(f'setoption name Threads value {threads}')
        # Stormphrax requires this for non-increment (nodes) TCs
        self._send('setoption name EnableWeirdTCs value true')
        self._send('isready')
        self._wait_for('readyok')

    def _send(self, cmd: str) -> None:
        self.proc.stdin.write(cmd + '\n')
        self.proc.stdin.flush()

    def _wait_for(self, token: str) -> str:
        while True:
            line = self.proc.stdout.readline().strip()
            if not line and self.proc.poll() is not None:
                raise RuntimeError(f'Engine {self.path!r} exited unexpectedly')
            if line.startswith(token):
                return line

    def new_game(self) -> None:
        self._send('ucinewgame')
        self._send('isready')
        self._wait_for('readyok')

    def set_position(self, fen: str, moves: list[str]) -> None:
        if moves:
            self._send(f'position fen {fen} moves {" ".join(moves)}')
        else:
            self._send(f'position fen {fen}')

    def go(self, nodes: int | None = None, depth: int | None = None,
           movetime: int | None = None) -> tuple[int | None, str | None]:
        """
        Search and return (score_cp_from_stm, bestmove_uci).
        Score is from the side-to-move's perspective (as reported by UCI).
        Returns (None, None) on engine failure.
        """
        if nodes is not None:
            self._send(f'go nodes {nodes}')
        elif depth is not None:
            self._send(f'go depth {depth}')
        elif movetime is not None:
            self._send(f'go movetime {movetime}')
        else:
            raise ValueError('Must specify nodes, depth, or movetime')

        last_cp: int | None = None
        last_mate: int | None = None
        bestmove: str | None = None

        while True:
            line = self.proc.stdout.readline().strip()
            if not line and self.proc.poll() is not None:
                return None, None
            if not line:
                continue

            if line.startswith('info') and ' score ' in line:
                parts = line.split()
                try:
                    si = parts.index('score')
                    kind = parts[si + 1]
                    val  = int(parts[si + 2])
                    if kind == 'cp':
                        last_cp, last_mate = val, None
                    elif kind == 'mate':
                        last_cp, last_mate = None, val
                except (ValueError, IndexError):
                    pass

            elif line.startswith('bestmove'):
                parts = line.split()
                if len(parts) >= 2 and parts[1] not in ('(none)', '0000'):
                    bestmove = parts[1]
                break

        if last_mate is not None:
            score = 3000 if last_mate > 0 else -3000
        elif last_cp is not None:
            score = last_cp
        else:
            score = None

        return score, bestmove

    def close(self) -> None:
        try:
            self._send('quit')
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


# â”€â”€ Game runner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# (fen, score_cp_stm, ply_index, side_to_move)
_PositionRecord = tuple[str, int, int, chess.Color]


def _play_game(
    e1: UCIEngine, e2: UCIEngine,
    start_fen: str,
    nodes: int | None,
    movetime: int | None,
) -> tuple[list[_PositionRecord], str]:
    """
    Play one game. Engine1 makes moves on even plies, engine2 on odd.
    (For a startpos with white to move: e1=white, e2=black.)

    Returns:
        records  â€” list of (fen, score_stm, ply, stm_color) BEFORE each move
        result   â€” '1-0' | '0-1' | '1/2-1/2' (from board perspective)
    """
    board = chess.Board(start_fen)
    moves: list[str] = []
    records: list[_PositionRecord] = []
    adj_count = 0
    adj_dir = 0     # +1 = white dominating, -1 = black dominating
    result = '1/2-1/2'

    e1.new_game()
    e2.new_game()

    for ply in range(MAX_HALFMOVES):
        # â”€â”€ Terminal check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if board.is_game_over(claim_draw=True):
            result = board.result(claim_draw=True)
            break

        # â”€â”€ Pick engine â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        engine = e1 if (ply % 2 == 0) else e2
        engine.set_position(start_fen, moves)
        score_stm, bestmove = engine.go(nodes=nodes, movetime=movetime)

        if bestmove is None:
            result = '1/2-1/2'
            break

        # â”€â”€ Validate move â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            move = chess.Move.from_uci(bestmove)
            if move not in board.legal_moves:
                result = '1/2-1/2'
                break
        except Exception:
            result = '1/2-1/2'
            break

        # â”€â”€ Record position before the move â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if score_stm is not None:
            records.append((board.fen(), score_stm, ply, board.turn))

        # â”€â”€ Adjudication â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if score_stm is not None:
            score_w = score_stm if board.turn == chess.WHITE else -score_stm
            if score_w >= ADJUDICATION_CP:
                d = 1
            elif score_w <= -ADJUDICATION_CP:
                d = -1
            else:
                d = 0

            if d != 0 and d == adj_dir:
                adj_count += 1
                if adj_count >= ADJUDICATION_N:
                    result = '1-0' if adj_dir == 1 else '0-1'
                    break
            else:
                adj_dir = d
                adj_count = 1 if d != 0 else 0

        board.push(move)
        moves.append(bestmove)
    else:
        result = '1/2-1/2'

    return records, result


# â”€â”€ Position encoder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_RESULT_WDL: dict[str, float] = {'1-0': 1.0, '0-1': 0.0, '1/2-1/2': 0.5}


def _encode(
    raw: list[_PositionRecord],
    result: str,
    skip_plies: int,
    result_blend: float,
    score_cap: int,
    skip_captures: bool,
) -> bytes:
    """Filter and encode a game's positions into RECORD_DTYPE binary blob."""
    result_wdl = _RESULT_WDL[result]
    out: list = []

    for fen, score_stm, ply, stm in raw:
        if ply < skip_plies:
            continue

        board = chess.Board(fen)

        if board.is_check():
            continue
        if skip_captures and any(board.is_capture(mv) for mv in board.legal_moves):
            continue

        # Convert score to white's perspective
        score_w = score_stm if stm == chess.WHITE else -score_stm
        score_w = max(-score_cap, min(score_cap, score_w))

        # Blend engine eval with game result
        sigmoid = 1.0 / (1.0 + math.exp(-score_w / WDL_SCALE))
        wdl = (1.0 - result_blend) * sigmoid + result_blend * result_wdl

        wf, bf, pc = board_features(board)
        if pc < 3:
            continue
        nw = min(len(wf), MAX_FEATS)
        nb = min(len(bf), MAX_FEATS)

        r = np.zeros(1, dtype=RECORD_DTYPE)[0]
        r['score']          = np.int16(int(score_w))
        r['wdl']            = np.float16(float(wdl))
        r['stm']            = 1 if stm == chess.BLACK else 0
        r['bucket']         = piece_count_bucket(pc)
        r['n_white']        = nw
        r['n_black']        = nb
        r['white_feats'][:nw] = wf[:nw]
        r['black_feats'][:nb] = bf[:nb]
        out.append(r)

    if not out:
        return b''
    return np.array(out, dtype=RECORD_DTYPE).tobytes()


# â”€â”€ Worker (runs in subprocess via ProcessPoolExecutor) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _worker(args: tuple) -> tuple[bytes, dict]:
    (e1_path, e2_path, n_games, nodes, movetime,
     openings, skip_plies, result_blend, score_cap,
     skip_captures, seed) = args

    rng = random.Random(seed)
    e1 = UCIEngine(e1_path, hash_mb=32, threads=1)
    e2 = UCIEngine(e2_path, hash_mb=32, threads=1)

    blob = b''
    stats: dict = {'games': 0, 'pos': 0,
                   'white_wins': 0, 'draws': 0, 'black_wins': 0}

    try:
        for i in range(n_games):
            fen = rng.choice(openings)
            # Alternate which engine has the first move (ensures balanced data
            # â€” engine1 doesn't always face the same pawn structure as white)
            if i % 2 == 0:
                raw, result = _play_game(e1, e2, fen, nodes, movetime)
            else:
                raw, result = _play_game(e2, e1, fen, nodes, movetime)

            data = _encode(raw, result, skip_plies, result_blend,
                           score_cap, skip_captures)
            blob += data
            stats['games'] += 1
            stats['pos']   += len(data) // RECORD_SIZE
            if result == '1-0':   stats['white_wins'] += 1
            elif result == '0-1': stats['black_wins'] += 1
            else:                 stats['draws']      += 1
    finally:
        e1.close()
        e2.close()

    return blob, stats


# â”€â”€ Opening book loader â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _load_openings(path: str | None) -> list[str]:
    """Load EPD/FEN positions from a file, or return built-in list."""
    if path is None:
        return list(BUILTIN_OPENINGS)
    openings: list[str] = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # EPD: FEN fields optionally followed by "; id ..." etc.
            fen_part = line.split(';')[0].strip()
            fields = fen_part.split()
            if len(fields) < 4:
                continue
            # Normalise: ensure move-number fields exist
            fen = ' '.join(fields[:6]) if len(fields) >= 6 else fen_part + ' 0 1'
            try:
                chess.Board(fen)     # validate
                openings.append(fen)
            except Exception:
                pass
    return openings if openings else list(BUILTIN_OPENINGS)


# â”€â”€ CLI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def main() -> None:
    p = argparse.ArgumentParser(
        description='Generate NNUE training data from engine self-play.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('--engine1', required=True,
                   help='Path to first UCI engine executable')
    p.add_argument('--engine2', required=True,
                   help='Path to second UCI engine executable')
    p.add_argument('--output',  required=True,
                   help='Output .bin path (RECORD_DTYPE binary)')
    p.add_argument('--games',   type=int, default=1000,
                   help='Total number of games to play')
    p.add_argument('--workers', type=int,
                   default=max(1, os.cpu_count() // 4),
                   help='Parallel game-pair processes')
    p.add_argument('--nodes',   type=int, default=20_000,
                   help='Search nodes per move')
    p.add_argument('--movetime', type=int, default=None,
                   help='Search time per move in ms (overrides --nodes)')
    p.add_argument('--skip-plies', type=int, default=10,
                   help='Ignore the first N plies of each game')
    p.add_argument('--result-blend', type=float, default=0.25,
                   help='Blend game result into WDL label (0=pure eval, 1=pure result)')
    p.add_argument('--score-cap', type=int, default=2000,
                   help='Clamp scores to Â±N cp before encoding')
    p.add_argument('--skip-captures', action='store_true', default=True,
                   help='Skip positions where captures are available (default on)')
    p.add_argument('--no-skip-captures', dest='skip_captures',
                   action='store_false')
    p.add_argument('--opening-book', default=None,
                   help='EPD opening book file (one FEN per line). '
                        'If omitted, uses 40-position built-in list.')
    p.add_argument('--seed', type=int, default=42)
    args = p.parse_args()

    for path in (args.engine1, args.engine2):
        if not Path(path).exists():
            print(f'ERROR: engine not found: {path}')
            sys.exit(2)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    openings = _load_openings(args.opening_book)
    nodes    = None if args.movetime else args.nodes
    movetime = args.movetime

    print(f'Engine 1:     {args.engine1}')
    print(f'Engine 2:     {args.engine2}')
    print(f'Games:        {args.games}')
    print(f'Workers:      {args.workers}')
    if movetime:
        print(f'Time/move:    {movetime} ms')
    else:
        print(f'Nodes/move:   {nodes:,}')
    print(f'Result blend: {args.result_blend}')
    print(f'Score cap:    Â±{args.score_cap} cp')
    print(f'Skip plies:   {args.skip_plies}')
    print(f'Skip captures:{args.skip_captures}')
    print(f'Openings:     {len(openings)}')
    print(f'Output:       {out_path}')
    print()

    # Distribute games across workers
    rng = random.Random(args.seed)
    base = args.games // args.workers
    extra = args.games % args.workers

    worker_args: list[tuple] = []
    for i in range(args.workers):
        n = base + (1 if i < extra else 0)
        worker_args.append((
            args.engine1, args.engine2, n,
            nodes, movetime, openings,
            args.skip_plies, args.result_blend,
            args.score_cap, args.skip_captures,
            rng.randint(0, 2 ** 31 - 1),
        ))

    # Placeholder header
    with open(out_path, 'wb') as f:
        f.write(b'\x00' * HEADER_SIZE)

    total_pos   = 0
    total_games = 0
    total_w     = 0
    total_d     = 0
    total_b     = 0
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_worker, a) for a in worker_args]
        for fut in as_completed(futures):
            try:
                blob, stats = fut.result()
            except Exception as e:
                print(f'  Worker failed: {e}')
                continue

            with open(out_path, 'ab') as f:
                f.write(blob)

            total_pos   += stats['pos']
            total_games += stats['games']
            total_w     += stats['white_wins']
            total_d     += stats['draws']
            total_b     += stats['black_wins']

            elapsed = time.time() - t0
            gps = total_games / max(elapsed, 0.01)
            pps = total_pos   / max(elapsed, 0.01)
            pct = total_games / max(args.games, 1) * 100
            print(f'  {total_games}/{args.games} games ({pct:.0f}%) | '
                  f'{total_pos:,} pos | {gps:.1f} g/s | {pps:.0f} pos/s | '
                  f'W/D/L {total_w}/{total_d}/{total_b}')

    # Write real header
    with open(out_path, 'r+b') as f:
        f.seek(0)
        f.write(struct.pack('<8sIII12x', HEADER_MAGIC, 1, total_pos, INPUT_SIZE))

    elapsed = time.time() - t0
    size_mb = (HEADER_SIZE + total_pos * RECORD_SIZE) / 1024 ** 2
    print()
    print(f'Done in {elapsed / 60:.1f}m')
    print(f'  Positions: {total_pos:,}')
    print(f'  Games:     {total_games}   W/D/L {total_w}/{total_d}/{total_b}')
    print(f'  Pos/game:  {total_pos // max(total_games, 1):.0f}')
    print(f'  Size:      {size_mb:.1f} MB')
    print(f'  Output:    {out_path}')
    print()
    print('To merge into training data:')
    print('  python tools/concat_bins.py data/processed/mean-alltime-dedup-shuffled.bin '
          f'{out_path} --output data/processed/merged.bin')


if __name__ == '__main__':
    main()
