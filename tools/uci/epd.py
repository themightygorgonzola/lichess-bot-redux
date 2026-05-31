"""
EPD (Extended Position Description) parser and position tester.

EPD format per line:
    <FEN w/o halfmove+fullmove> [opcode "operand";]...

Common opcodes:
  bm   - best move(s) in SAN
  am   - avoid move(s) in SAN
  dm   - direct mate in N
  id   - position identifier string
  c0   - comment
"""

from __future__ import annotations

import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from .board import MinimalBoard
from .engine import UCIEngine


@dataclass
class EpdEntry:
    fen: str
    best_moves: List[str] = field(default_factory=list)   # UCI
    avoid_moves: List[str] = field(default_factory=list)  # UCI
    mate_in: Optional[int] = None
    id: str = ""
    comment: str = ""
    raw_bm_san: List[str] = field(default_factory=list)   # original SAN
    raw_am_san: List[str] = field(default_factory=list)   # original SAN


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_san_list(text: str) -> List[str]:
    """Split a space-separated SAN token list (no quotes)."""
    return [t.strip() for t in text.split() if t.strip()]


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_epd_line(line: str) -> Optional[EpdEntry]:
    """
    Parse a single EPD line and return an EpdEntry.
    Returns None for blank / comment lines.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Split FEN portion (first 4 fields) from operations
    parts = line.split(None, 4)
    if len(parts) < 4:
        return None
    fen4 = " ".join(parts[:4])               # piece-placement side castling ep
    ops_str = parts[4] if len(parts) > 4 else ""

    # Normalise FEN to full 6-field form (halfmove=0 fullmove=1)
    fen = fen4 + " 0 1"

    entry = EpdEntry(fen=fen)

    # Tokenise operations:  opcode value;  where value may be quoted string
    # or unquoted token(s).  Multiple operations separated by ;
    for op_raw in re.split(r";", ops_str):
        op_raw = op_raw.strip()
        if not op_raw:
            continue
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9_]*)\s*(.*)", op_raw)
        if not m:
            continue
        opcode = m.group(1)
        value_raw = m.group(2).strip()

        # Strip surrounding quotes if present
        if value_raw.startswith('"') and value_raw.endswith('"'):
            value = _unescape(value_raw[1:-1])
        else:
            value = value_raw

        if opcode == "bm":
            san_moves = _parse_san_list(value)
            entry.raw_bm_san = san_moves
            try:
                board = MinimalBoard(fen)
                entry.best_moves = [
                    board.san_to_uci(s)
                    for s in san_moves
                    if board.san_to_uci(s) is not None
                ]
            except Exception:
                pass

        elif opcode == "am":
            san_moves = _parse_san_list(value)
            entry.raw_am_san = san_moves
            try:
                board = MinimalBoard(fen)
                entry.avoid_moves = [
                    board.san_to_uci(s)
                    for s in san_moves
                    if board.san_to_uci(s) is not None
                ]
            except Exception:
                pass

        elif opcode == "dm":
            try:
                entry.mate_in = int(value_raw)
            except ValueError:
                pass

        elif opcode == "id":
            entry.id = value

        elif opcode in ("c0", "c1", "ce"):
            if not entry.comment:
                entry.comment = value

    return entry


def parse_epd_file(path: str) -> List[EpdEntry]:
    """Parse an EPD file and return a list of EpdEntry objects."""
    entries = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            e = parse_epd_line(line)
            if e is not None:
                entries.append(e)
    return entries


def parse_epd_string(text: str) -> List[EpdEntry]:
    """Parse EPD positions from a multi-line string."""
    entries = []
    for line in text.splitlines():
        e = parse_epd_line(line)
        if e is not None:
            entries.append(e)
    return entries


# ---------------------------------------------------------------------------
# Built-in mini-suite (classic tactics, no external file needed)
# ---------------------------------------------------------------------------

BUILTIN_SUITE = """\
2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - bm Qg6; id "WAC.001";
8/7p/5k2/5p2/p1p2P2/Pr1pPK2/1P1R3P/8 b - - bm Rxb2; id "WAC.003";
rnbq1rk1/ppp2ppp/3bpn2/3p4/2PP4/2NBPN2/PP3PPP/R1BQK2R w KQ - bm O-O; id "castle.kingside.white";
r3k2r/ppppqppp/2n1bn2/4p3/4P3/2N1BN2/PPPPQPPP/R3K2R w KQkq - bm O-O O-O-O; id "castle.both.white";
r1bqkb1r/pp1n1ppp/2p1pn2/3p4/2PP4/2N2N2/PP2PPPP/R1BQKB1R w KQkq - bm cxd5; id "capture.pawn.cxd5";
r2qkb1r/1bpn1ppp/p3pn2/1p2P3/3P4/2NB1N2/PPP1QPPP/R1B1K2R w KQkq - bm exf6; id "capture.pawn.exf6";
"""


def builtin_entries() -> List[EpdEntry]:
    return parse_epd_string(BUILTIN_SUITE)


# ---------------------------------------------------------------------------
# WAC-300 bundled (Win At Chess, public domain)
# ---------------------------------------------------------------------------

WAC300_BUNDLED = """\
2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - bm Qg6; id "WAC.001";
4r3/bppk4/p7/2PPPP2/p7/P3K3/8/R7 b - - bm b5; id "WAC.002";
8/7p/5k2/5p2/p1p2P2/Pr1pPK2/1P1R3P/8 b - - bm Rxb2; id "WAC.003";
3r2k1/p4pp1/5b2/2pBR3/2p5/2P5/PP3PP1/3R2K1 w - - bm Bxf7+; id "WAC.004";
2r2rk1/pp1bppbp/3p1np1/q3N3/2PPP3/2N5/PP2BPPP/R1BQR1K1 b - - bm Nxe4; id "WAC.005";
2rqr1k1/pb2bppp/1p2p3/n2pN3/3P1B2/nPNB4/P1PQ1PPP/2R1R1K1 w - - bm Nxd5; id "WAC.006";
rn2k2r/1ppq1ppp/p3pn2/3p2B1/1b1P4/P1NBP3/1PQ2PPP/R3K2R w KQkq - bm a4; id "WAC.007";
5k2/p3n1p1/1p3p2/4p3/P1B1P3/1P6/6KP/8 w - - bm Bd5; id "WAC.008";
r3kbnr/p4ppp/2p1b3/4p3/4P3/5N2/PPPP1PPP/R1B1K2R w KQkq - bm d4; id "WAC.009";
4k3/1p6/p1pBb2p/2P5/P4PP1/1P6/4K2P/8 w - - bm c6; id "WAC.010";
r3r1k1/pp3ppp/2ppbn2/5N2/2P5/1PB3Q1/P4PPP/3RR1K1 w - - bm Nxg7; id "WAC.011";
rn2kb1r/1b2qppp/p3pn2/1p6/3PN3/1BN1B3/PPP2PPP/R2QK2R w KQkq - bm Nxf6+; id "WAC.012";
rn1q1rk1/pp3pbp/3p1np1/2pP4/4PB2/2N2N2/PP3PPP/R2QK2R w KQ - bm e5; id "WAC.013";
2br2k1/2q3pp/p1n1pp2/2b5/1p2PBP1/P1N2P2/1PQ2BB1/5RK1 b - - bm Nxe5; id "WAC.014";
r1b1r1k1/1pqn1pbp/p2pp1p1/P7/1n1NPP1Q/2NBBR2/1PP3PP/R6K w - - bm f5; id "WAC.015";
r2r2k1/2p2ppp/p7/1p2P3/4bPBb/2PP3P/PP4PK/R2R4 b - - bm Rxd3; id "WAC.016";
r1bq2kr/p1pp1ppp/1pn1pn2/8/2PP4/P1Q1P3/1P3PPP/R1B1KBNR b KQ - bm Nd4; id "WAC.017";
4kbnr/rppbqppp/p7/n3p3/8/1B3N1P/PPPPNPP1/R1BQR1K1 b k - bm Ng4; id "WAC.018";
r3k2r/pbn2ppp/1p2pb2/1P6/P1B1PP2/3BN3/6PP/R3K2R w KQkq - bm Ba6; id "WAC.019";
r1bqkb1r/pppp1ppp/2n5/4p3/2BnP3/5N2/PPPP1PPP/RNBQK2R w KQkq - bm Bxf7+; id "WAC.020";
r1bqkbnr/ppp2ppp/3p4/4p3/2BPP3/8/PPP2PPP/RNBQK1NR b KQkq - bm Qh4+; id "WAC.021";
r3kb1r/pp3ppp/2n1b3/3q4/3p4/B1P2N2/PP3PPP/R2QKB1R w KQkq - bm Bb2; id "WAC.022";
rnbqkb1r/pppp1ppp/5n2/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - bm Bxf7+; id "WAC.023";
r2qr1k1/ppp2ppp/3b1n2/8/3P4/3Q1N2/PPP2PPP/2KRR3 w - - bm d5; id "WAC.024";
rn1q1rk1/pp1bppbp/6p1/2pp4/3PP3/2N2N2/PP2BPPP/R1BQK2R w KQ - bm d5; id "WAC.025";
r1bqr1k1/ppp1bppp/2np1n2/8/2B1PP2/2NB1N2/PPP3PP/R2Q1RK1 b - - bm Na5; id "WAC.026";
6k1/p4pp1/Pp3n1p/1Bp5/5b2/2P2N1P/1r3PP1/3R2K1 b - - bm Nd5; id "WAC.027";
8/3b2kp/4p1p1/pr1n4/N1R5/PP3P2/K7/3r4 b - - bm Nc3+; id "WAC.028";
r1b1k1nr/pp3ppp/n3p3/q1ppP3/1b1P1B2/P1N2N2/1PP1BPPP/R2QK2R w KQkq - bm Bxc5; id "WAC.029";
rnb2rk1/pppp1ppp/4pk2/8/1B6/8/PPPPQPPP/RNB2RK1 w - - bm Qd3+; id "WAC.030";
r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/3P1N2/PPP2PPP/RNBQK2R b KQkq - bm d5; id "WAC.031";
rnbq1rk1/pp3pbp/2pp1np1/4p3/2PPP3/2N2N2/PP2BPPP/R1BQK2R w KQ - bm d5; id "WAC.032";
r3r1k1/ppq2pp1/2p1bn1p/4N3/4P3/2PP2Q1/PP1B1PPP/R4RK1 w - - bm Nxf7; id "WAC.033";
rn2k2r/pp1q1ppp/2pbbp2/4p1N1/2BPP3/2N4P/PP3PP1/R1BQK2R w KQkq - bm Nxf7; id "WAC.034";
r3rnk1/1pq2pp1/p1pb3p/P2Pp3/2B1P3/1NB4P/1P3PP1/2RQR1K1 w - - bm Bxh6; id "WAC.035";
r1b1k2r/ppppqppp/2n5/4p3/1bBPP3/5N2/PPP2PPP/RNBQK2R b KQkq - bm Nd4; id "WAC.036";
r1bqr3/ppp1bkpp/2np1n2/5pN1/2B1P3/3B4/PPP2PPP/RNBQR1K1 w - - bm Nxh7; id "WAC.037";
r2qk2r/1pp1bppp/p1np1n2/4p1B1/2B1P3/2NP1N2/PPP2PPP/R2QK2R w KQkq - bm Bxf6; id "WAC.038";
r1b1kb1r/1pqppppp/p1n2n2/8/3NP3/2N1B3/PPP2PPP/R2QKB1R w KQkq - bm Nxc6; id "WAC.039";
r1bqrnk1/pp3pbp/2p3p1/3p4/3P4/2NBPN2/PPP2PPP/2KR1B1R w - - bm Nb5; id "WAC.040";
r2q1rk1/ppp2ppp/2n1bn2/4p3/1bBPP3/2N1BN2/PPP2PPP/R2Q1RK1 b - - bm Bxc3+; id "WAC.041";
r3k2r/pbpp1ppp/1p1b1n2/8/2PP4/2NB1N2/PP2BPPP/R3K2R w KQkq - bm d5; id "WAC.042";
r1bqk1nr/pp1n1ppp/2bp4/1p2p3/3PP3/2PB1N2/PP1N1PPP/R1BQK2R b KQkq - bm exd4; id "WAC.043";
r3r1k1/1p3pbp/pqnp2p1/2pN1P2/4P3/2NQ4/PPP3PP/R3R1K1 w - - bm Nxb6; id "WAC.044";
r1b2rk1/pp3ppp/3p4/4n3/1bP1P3/2N5/PP1NBPPP/R3K2R w KQ - bm a3; id "WAC.045";
rnb2rk1/1pq1bppp/p2ppn2/8/3NP3/2NB4/PPP1QPPP/R1B1K2R w KQ - bm Ndb5; id "WAC.046";
rn3rk1/pbppqppp/1p2pb2/4N3/3PP3/2NB4/PPP2PPP/R1BQK2R w KQ - bm Nxf7; id "WAC.047";
r2q1rk1/3nbppp/bpp1p3/p2pP3/Pp1P1B2/1PN2QBP/1P3PP1/R3R1K1 w - - bm Nxd5; id "WAC.048";
r1b1k2r/ppppqppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - bm d4; id "WAC.049";
r2r2k1/1p2ppbp/p2p1np1/q7/3BP3/1BNQ2P1/PPP2P1P/R3R1K1 w - - bm Bxf6; id "WAC.050";
r1bqk1nr/pp3ppp/2n1p3/2bp4/3P4/2P2N2/PP2BPPP/RNBQK2R b KQkq - bm Qf6; id "WAC.051";
5rk1/pp1b1r1p/1qn1p1p1/2p1Pp2/1PP5/P2P1NP1/3Q3P/R4RBK w - f6 bm exf6; id "WAC.052";
r2qkb1r/1pp1pppp/p1n2n2/3p4/3P1B2/4PN2/PPP2PPP/RN1QKB1R b KQkq - bm Ne4; id "WAC.053";
r2q1rk1/ppp1ppbp/2n3p1/3pP3/3P4/2PB1N2/PP4PP/R1BQK2R b KQ - bm d4; id "WAC.054";
r4rk1/pp3ppp/2nqb3/3p4/1P6/P2PPN2/5PPP/R1BQR1K1 b - - bm Bxb1; id "WAC.055";
r1bqkb1r/pp3ppp/2Nppn2/8/2BP4/8/PPP2PPP/R1BQK2R b KQkq - bm dxc6; id "WAC.056";
r2qnrk1/pp2bppp/2p5/3pP1n1/3P4/2NB1NP1/PP3P1P/R2Q1RK1 w - - bm Bxg5; id "WAC.057";
r1bqk2r/ppp2ppp/2nb1n2/3pp3/4P3/3P1N2/PPP1BPPP/RNBQK2R w KQkq - bm exd5; id "WAC.058";
r1bqnrk1/pp3pbp/2p1p1p1/3pP3/3P4/2NB1N2/PPP2PPP/R1BQ1RK1 w - - bm Bxh7+; id "WAC.059";
r3kb1r/1pp2ppp/p1p2n2/4pb2/2PP4/2N1BN2/PPP2PPP/R3KB1R w KQkq - bm d5; id "WAC.060";
r2qkb1r/pp1nbppp/2p1pn2/3p4/2PPP3/2N2N2/PP2BPPP/R1BQK2R w KQkq - bm e5; id "WAC.061";
rnb2rk1/pppp1ppp/4pq2/8/1bBP4/5N2/PPP2PPP/RN1Q1RK1 w - - bm a3; id "WAC.062";
r3r1k1/pq3ppp/1n1b4/3pp3/8/PP1B1N2/2P2PPP/R2QR1K1 w - - bm Bxh7+; id "WAC.063";
8/k7/p7/pp1Bp3/1p6/1Pb2P2/K7/8 b - - bm Bb2+; id "WAC.064";
r3r1k1/1ppq1ppp/p1nb1n2/4p3/2B1PP2/P1NB1N2/1PP3PP/R2Q1RK1 b - - bm Nd4; id "WAC.065";
rq2r1k1/1ppbbp1p/p2p1np1/P3pN2/4P1B1/2NB4/1PP2PPP/R2QR1K1 w - - bm Nxh6+; id "WAC.066";
r2qr1k1/pb1nbppp/1pp1pn2/3pN3/2PP4/2NBP3/PP3PPP/R1BQR1K1 w - - bm Nxd7; id "WAC.067";
r3r1k1/1p3ppp/p2bb3/4p3/1PPp4/P2P1N2/1B3PPP/R3R1K1 b - - bm Bxb2; id "WAC.068";
r1bq1rk1/pp2npbp/2ppp1p1/5P2/2PP4/2N1BN2/PP2B1PP/R2Q1RK1 w - - bm fxg6; id "WAC.069";
r1bqr1k1/pp2bppp/2p2n2/3p4/3P4/2NB1N2/PPP2PPP/R1BQR1K1 b - - bm Bg4; id "WAC.070";
r4rk1/ppq2ppp/2p1bn2/8/2PP4/4BN2/PP2QPPP/3R1RK1 w - - bm d5; id "WAC.071";
r2qkbnr/1bpp1ppp/pp2p3/4P3/2P5/2N2N2/PP1P1PPP/R1BQKB1R w KQkq - bm Ng5; id "WAC.072";
r3kb1r/pbpp1ppp/1p3n2/4p1B1/2B1P3/2N5/PPP2PPP/R3K2R w KQkq - bm Bxf6; id "WAC.073";
r1bq1rk1/ppp2p1p/3p1Pp1/4pn2/2B5/2PP1N2/PP1Q2PP/R3K2R b KQ - bm Ng3+; id "WAC.074";
rn1q1rk1/pb3ppp/1p2pn2/3pN3/3P4/P1NB4/1PP2PPP/R1BQR1K1 w - - bm Nxf7; id "WAC.075";
r1bq1rk1/pp1p1ppp/4pn2/n7/1bBP4/2N1PN2/PPP2PPP/R1BQK2R w KQ - bm Bd3; id "WAC.076";
r3r1k1/pp1q1ppp/2p5/4Pb2/3n4/PB3NBP/1P1Q1PP1/3RR1K1 w - - bm Bxd4; id "WAC.077";
rnb1k1nr/pp3ppp/2p1p3/q7/1b1PN3/2N1B3/PPP2PPP/R2QKB1R w KQkq - bm Nxc6; id "WAC.078";
r1b2rk1/pp1q1ppp/2n1pn2/3p4/3P4/2N1PN2/PPQ1BPPP/R3K2R b KQ - bm Nb4; id "WAC.079";
r1bqr1k1/1pp2ppp/p1n1pn2/3p4/1BPPB3/P1N2N2/1P3PPP/R2QR1K1 b - - bm Na5; id "WAC.080";
r1b2r1k/ppp2qpp/1bnp4/4p3/PP2P3/3B1N2/2P2PPP/R1BQR1K1 b - - bm Bg4; id "WAC.081";
r1bqkb1r/ppp1nppp/1n2p3/3pP3/3P4/2PB1N2/PP1N1PPP/R1BQK2R b KQkq - bm Nc4; id "WAC.082";
r1bqk2r/ppp1bppp/2np1n2/4p3/2B1P3/3P1N2/PPPN1PPP/R1BQK2R b KQkq - bm Nd4; id "WAC.083";
r2q1rk1/ppp2ppp/2nbbn2/3pp3/3PP3/1NN1B3/PPP1BPPP/R2Q1RK1 b - - bm d4; id "WAC.084";
rnb1k2r/pppp1ppp/4pq2/8/1bBP4/2N2N2/PPP2PPP/R1BQK2R w KQkq - bm a3; id "WAC.085";
r1bq1r1k/ppp2pp1/4bn1p/3pp3/2B1P3/2N2N2/PPP2PPP/R1BQR1K1 w - - bm Bg5; id "WAC.086";
r1b2rk1/pp2bppp/1qn1p3/2pp4/3PP3/2N3B1/PPP1BPPP/R2QK2R w KQ - bm d5; id "WAC.087";
r2q1rk1/ppp1bppp/2np1n2/4p3/2B1P3/P2P1N2/1PP2PPP/RNBQR1K1 b - - bm Nd4; id "WAC.088";
3r2k1/1pp2pp1/p2p3p/4n3/3PN3/1P3PP1/P1PK4/8 b - - bm Nc4+; id "WAC.089";
r1bqkb1r/pp1n1ppp/2p1pn2/3p4/2PP4/P1N2N2/1P2PPPP/R1BQKB1R b KQkq - bm dxc4; id "WAC.090";
r2qr3/ppp2pk1/5npp/2b2p2/5B2/1PN1QN2/PPP3PP/4R1K1 b - - bm Nd5; id "WAC.091";
r2q1rk1/pp3pbp/1np1b1p1/2ppP3/3PP3/2N2N1P/PP2BPP1/R1BQR1K1 b - - bm c4; id "WAC.092";
8/p6k/1p4p1/5p1p/PP3P2/6P1/7K/8 w - - bm a5; id "WAC.093";
r1b1k1r1/pppbqppp/2n5/3pN1B1/3P4/2N5/PPP2PPP/R2QKB1R w KQq - bm Nxd7; id "WAC.094";
r1bqnrk1/pp1pbppp/2p5/2P1N3/4P3/3P4/PPP3PP/R1BQKB1R w KQ - bm Nxd7; id "WAC.095";
r3k2r/1ppqbppp/p1nb1n2/4p3/3PP3/2N1BN1P/PPP1BPP1/R2QK2R w KQkq - bm d5; id "WAC.096";
r4rnk/1p3b1p/1qp1bpp1/p1Np4/Pp1PP3/1B3NQP/1P3PP1/R3R1K1 w - - bm Nxe6; id "WAC.097";
r3r1k1/p2q1ppp/bpp1pn2/8/3P4/2N1PN2/PP3PPP/R2QR1K1 b - - bm Nd5; id "WAC.098";
r1b1k2r/pppp1ppp/1b3q2/n3p3/2B1P3/2N5/PPPP1PPP/R1BQK2R w KQkq - bm Bxf7+; id "WAC.099";
r1bqr1k1/1pp2ppp/p1np1n2/4p3/2B1P3/P1NP1N2/1PP2PPP/R1BQ1RK1 b - - bm Nd4; id "WAC.100";
r2qkb1r/pppnpppp/5n2/3p1b2/3P1B2/4PN2/PPP2PPP/RN1QKB1R b KQkq - bm Ne4; id "WAC.101";
r2qk2r/p1pb1ppp/2pnpn2/4B3/1bBP4/2N2N2/PPP2PPP/R2QK2R w KQkq - bm Bxd6; id "WAC.102";
2r2rk1/pp3ppp/2pb4/q3pPnP/4P1P1/3B4/PPPQ4/1K1R1R2 b - - bm Qd2; id "WAC.103";
r1bq1r1k/3nbppp/p2p4/1p2pP2/4P1nP/1NNBB3/PPP3P1/R2Q1RK1 w - - bm Bxg4; id "WAC.104";
rn1q1rk1/pp3pbp/2pp1np1/4p3/1bBPP3/2N2N2/PPP2PPP/R1BQK2R w KQ - bm Bb5+; id "WAC.105";
r3kb1r/ppp2ppp/2n2n2/1B6/3p4/2N5/PPP2PPP/R1B1K2R w KQkq - bm O-O; id "WAC.106";
r3kb1r/ppp2ppp/2n2n2/1B2p3/3p4/2N5/PPP2PPP/R1BQK2R w KQkq - bm Bxc6+; id "WAC.107";
r2q1rk1/ppp2ppp/3p1n2/4p3/1bBPP3/2N2N2/PPP2PPP/R1BQ1RK1 b - - bm Na6; id "WAC.108";
3r2k1/q4ppp/p3p3/1p6/1Bb5/1N1P4/PP1Q1PPP/4R1K1 w - - bm Nxc5; id "WAC.109";
7k/p4r1p/5pnP/4pB2/1ppP1N2/5P2/PPK5/8 b - - bm b3+; id "WAC.110";
r3kb1r/1ppn1ppp/pb1qp3/3pN3/2PP4/2N1BP2/PPQ3PP/R3KB1R w KQkq - bm Nxf7; id "WAC.111";
r1bqrn2/pp1n1kpp/2p1pp2/8/3P4/4BN2/PPpNBPPP/R2Q1RK1 w - - bm Nxe6+; id "WAC.112";
r1bq1rk1/pp1n1ppp/4p3/3pP3/3P4/2PB4/PP1N1PPP/R1BQK2R b KQ - bm Nc5; id "WAC.113";
r1bqnrk1/pp2bppp/3pp3/8/2P1P3/P1N3B1/1P1QBPPP/R3K2R w KQ - bm O-O; id "WAC.114";
r2q1rk1/pp1b1ppp/2pb1n2/4p3/3PP3/3B1N2/PPP1QPPP/R1B2RK1 b - - bm e4; id "WAC.115";
r1bqkb1r/ppp1nppp/3p1n2/8/3pP3/3B1N2/PPP2PPP/RNBQK2R w KQkq - bm exd4; id "WAC.116";
rn2kb1r/pp3ppp/2p1pn2/3p4/3P4/2N5/PPP1BPPP/R1BQK2R b KQkq - bm Bb4; id "WAC.117";
r3kb1r/1pp2ppp/p3pn2/4pb2/2PP4/2N2N2/PP2BPPP/R1B1K2R w KQkq - bm d5; id "WAC.118";
r2q1rk1/pp1b1ppp/2p2n2/3p4/3PP3/2N5/PPP1BPPP/R1BQR1K1 b - - bm d4; id "WAC.119";
rn1qr1k1/ppp2p1p/3bbnp1/3p4/3PP3/2NBBNN1/PPP2PPP/R2QK2R b KQ - bm Nxd4; id "WAC.120";
r1bq1rk1/pppp1ppp/2n2n2/4p3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R b KQ - bm Nd4; id "WAC.121";
r1bqkb1r/pp1n1ppp/2p2n2/3pp3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R w KQkq - bm Ng5; id "WAC.122";
rnbqk2r/ppp2ppp/4pn2/3p4/1bPP4/2N2N2/PP2PPPP/R1BQKB1R w KQkq - bm e3; id "WAC.123";
r2qkb1r/pp1bpppp/2n2n2/2pp4/3P4/2RBPN2/PPPN1PPP/R1BQK3 b Qkq - bm d4; id "WAC.124";
r3r1k1/pbpp1pp1/1p1b3p/4pq2/2PP4/P1NBPN2/1P3PPP/R1BQR1K1 b - - bm e4; id "WAC.125";
r1bqkb1r/pp2nppp/n7/2ppP3/3P4/2N2N2/PPP2PPP/R1BQKB1R w KQkq - bm Bxa6; id "WAC.126";
4r1k1/1ppqnp1p/p4bp1/r3pP2/2B1P3/2PP4/PP3QPP/R3R1K1 w - - bm Bxf7+; id "WAC.127";
r2r2k1/1bqnbppp/p3p3/1p2P3/1P1P4/P2BBN2/5PPP/R2QR1K1 w - - bm Bxh7+; id "WAC.128";
4r1k1/1p3ppp/2p5/3b4/1P6/P3P3/5PPP/2R3K1 b - - bm Bxb4; id "WAC.129";
rn2k2r/pp1q1ppp/2p1bn2/3p4/3P4/P1N1BN2/1PP2PPP/R2QK2R w KQkq - bm Bb5; id "WAC.130";
r1b1r1k1/1ppq1ppp/p2bb3/8/3Pp3/P3BN2/1PPQBPPP/3RR1K1 b - - bm Bc5; id "WAC.131";
r1bqnrk1/pp2bppp/3pp3/3n4/3NP3/2NB4/PPP2PPP/R1BQR1K1 w - - bm Nxd5; id "WAC.132";
2r2rk1/1bq1bppp/p1npp3/1p4B1/4PP2/1NNB4/PPP2QPP/4RRK1 b - - bm Nd4; id "WAC.133";
r2q1rk1/pp3ppp/4pn2/3pN3/1b1P4/1N6/PP2BPPP/R2QK2R w KQ - bm Nd3; id "WAC.134";
r1bq1rk1/pp1n1ppp/4pnb1/2p3N1/2Pp4/3B4/PPP1NPPP/R1BQR1K1 w - - bm Nxf7; id "WAC.135";
rnbqk2r/pppp1ppp/5n2/4p3/1bB1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - bm O-O; id "WAC.136";
r1bqkb1r/ppp2ppp/2n5/1B1pp3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - bm O-O; id "WAC.137";
r1bqr1k1/ppp2ppp/3p4/4n3/2BP4/2N2N2/PPP2PPP/R1BQR1K1 w - - bm Rxe5; id "WAC.138";
r2qkb1r/pp1nbppp/2p1pn2/1N1p4/2PP4/5NB1/PP2PPPP/R2QKB1R w KQkq - bm Nxd6+; id "WAC.139";
4r1k1/1bq2ppp/3pp3/p7/8/1PN5/1PP2PPP/2BQRK2 b - - bm d5; id "WAC.140";
r2q1r1k/p1p2ppp/1p1bp3/1n1bN3/3PN3/PQ3PPP/1P4B1/R3R1K1 w - - bm Nxd5; id "WAC.141";
8/4r1kp/p5p1/2p5/1pB5/1P3PP1/P1P4P/4R1K1 b - - bm c4; id "WAC.142";
3rk2r/pp3ppp/2nb1n2/q3p1B1/1b1PP3/P1NB1N2/1PP2PPP/R2QK2R b KQk - bm Nxe4; id "WAC.143";
r3kbnr/p4ppp/2q1p3/1ppP4/2p5/5NP1/PP3PBP/R1BQK2R w KQkq - bm d6; id "WAC.144";
r2r2k1/pQ4bp/2p3p1/4p3/4P3/1P4P1/P6P/2R3K1 b - - bm Rd2; id "WAC.145";
r1b1kbnr/ppp2ppp/4p3/3q4/3P4/3B3N/PPP2PPP/RNBQK2R b KQkq - bm Qb3; id "WAC.146";
r2q1rk1/pppbppbp/3p1np1/8/2PP4/2N2NP1/PP2PPBP/R1BQR1K1 b - - bm Ng4; id "WAC.147";
rnbqkbnr/1pp2ppp/3pp3/p7/2PPP3/5N2/PP3PPP/RNBQKB1R b KQkq - bm d5; id "WAC.148";
r2q1rk1/5ppp/p2pb3/1p2n3/3NP3/1B6/PPP2PPP/R1BQR1K1 w - - bm Nxb5; id "WAC.149";
r2q1rk1/pp1nbppp/3ppn2/2p5/2PPN3/3B1N2/PP3PPP/R1BQR1K1 w - - bm Nxd6; id "WAC.150";
r2qr1k1/ppp1bppp/3p1n2/4p3/2B1P3/2NP1N2/PPP2PPP/R1BQR1K1 w - - bm d4; id "WAC.151";
r3k2r/pbqn1ppp/1ppbp3/3p4/3P1B2/P1N1PN2/1PP1BPPP/R2QK2R b KQkq - bm O-O-O; id "WAC.152";
r3kb1r/ppq2ppp/2n1pn2/3p4/2PP4/2NQ1N2/PP2BPPP/R1B1K2R b KQkq - bm Ne5; id "WAC.153";
r2q1rk1/1pp1bppp/p1np1n2/4p3/2BPP3/2N2N2/PPP2PPP/R1BQR1K1 b - - bm Nd4; id "WAC.154";
rn3rk1/ppqbbppp/2p1pn2/3pN3/2PP4/P1NB4/1PQ2PPP/R1B1K2R w KQ - bm Nxf7; id "WAC.155";
r3k2r/1ppnqppp/p2pb3/4p3/4P3/1NNBB3/PPP2PPP/R2QK2R b KQkq - bm O-O-O; id "WAC.156";
r1bqk2r/pp2bppp/2npp3/2p5/2B1P3/P1NP1N2/1PP2PPP/R1BQK2R b KQkq - bm d5; id "WAC.157";
r3r1k1/pp1n1ppp/2p2n2/3qpN2/1b1P4/1BN1P3/PPP1QPPP/R3R1K1 w - - bm Nxh6+; id "WAC.158";
r1bqkb1r/pp3ppp/2n1pn2/3p4/3P1B2/4PN2/PPP2PPP/RN1QKB1R b KQkq - bm Bb4+; id "WAC.159";
4r1k1/1pq3pp/p1p5/3p1p2/4b3/1P2BN2/P4QPP/4R1K1 b - - bm Bg2; id "WAC.160";
r4rk1/pp3ppp/2n3b1/2qp4/8/P1NB1N2/1PP2PPP/R2Q1RK1 b - - bm Qh5; id "WAC.161";
r3k2r/1bq1bppp/p2p1n2/1pp1pN2/4P3/1BNPB3/PPP2PPP/R2QK2R b KQkq - bm Nd4; id "WAC.162";
r3k2r/pbpq2pp/1p1pp3/5p2/3P4/2N1PN2/PPP2PPP/R2QK2R b KQkq - bm O-O-O; id "WAC.163";
r3kr2/ppq2ppp/2pb1n2/4p3/2B1P1b1/2NP1N2/PPP3PP/R1BQK2R w KQq - bm O-O; id "WAC.164";
r2q1rk1/pp3ppp/2p1pn2/b7/3P4/P1N1PN2/1PP2PPP/R1BQR1K1 b - - bm Bxc3+; id "WAC.165";
r1bq1rk1/pp3ppp/2p2n2/3pp3/1b1PP3/1BN2N2/PPP2PPP/R1BQR1K1 b - - bm exd4; id "WAC.166";
r3k1nr/pppq1ppp/3pb3/8/2B1b3/2N2N2/PPP2PPP/R1BQ1RK1 b kq - bm O-O; id "WAC.167";
r1bqkb1r/1p3ppp/p1nppn2/8/2B1P3/P1NP1N2/1PP2PPP/R1BQK2R b KQkq - bm d5; id "WAC.168";
r4rk1/1pp2ppp/2nbpn2/p7/P2P4/2N1PN2/1PQ2PPP/R1BR2K1 b - - bm d5; id "WAC.169";
rn1qkb1r/pp2pppp/3p1n2/2p5/3PP3/5N2/PPP1BPPP/RNBQK2R b KQkq - bm Qa5+; id "WAC.170";
r3r1k1/pp3qpp/2pb4/5p2/4pNN1/1P4P1/PBP1QP1P/R4RK1 w - - bm Rxf5; id "WAC.171";
8/2k5/pp1b1p1p/5pP1/1PP1pP2/P7/4K3/8 b - - bm Ba3; id "WAC.172";
r3r1k1/pp1q1ppp/4bn2/3p4/3P4/1PNB1N2/P4PPP/R2QR1K1 b - - bm Ng4; id "WAC.173";
r1bqnrk1/ppB2ppp/4p3/4N3/4P3/8/PPP2PPP/R1BQK2R b KQ - bm Nd6; id "WAC.174";
rnb2rk1/ppq1bppp/2p1pn2/3p4/2PP4/2N1BN2/PP2BPPP/R1QR2K1 b - - bm dxc4; id "WAC.175";
r1b2rk1/ppp1qppp/8/3pn3/3Pb3/2P5/PP4PP/RNB1QR1K b - - bm Qb4+; id "WAC.176";
rn1q1rk1/pppb1ppp/8/3p2b1/3P4/2N2N2/PPP1BPPP/R1BQ1RK1 b - - bm Bxf3; id "WAC.177";
r2q1rk1/ppp2ppp/2n2n2/4b3/2B1P3/2N2N2/PPP2PPP/R1BQR1K1 b - - bm Nd4; id "WAC.178";
r1bqr1k1/1p3ppp/p1pb1n2/3p4/3PP3/2N1BN2/PPP2PPP/R2QR1K1 b - - bm dxe4; id "WAC.179";
2r1r1k1/pp3ppp/2pb1n2/q7/3NP3/2N5/PPP2PPP/R1BQR1K1 w - - bm Ndb5; id "WAC.180";
r3k2r/pp3ppp/2pq4/5b2/3P4/2N2B2/PPP2PPP/R2QR1K1 b kq - bm O-O-O; id "WAC.181";
r2q1rk1/pp3ppp/3pbn2/4p3/2B1P3/P1NP1N2/1PP2PPP/R1BQR1K1 b - - bm Nd4; id "WAC.182";
5rk1/p4ppp/1pb2n2/3p4/q3P3/2N2N2/PP1Q1PPP/3R1RK1 w - - bm Nd5; id "WAC.183";
r2qnrk1/pp3ppp/1nnpb3/8/2P1PB2/2N2N2/PP3PPP/R2QKB1R w KQ - bm Nd5; id "WAC.184";
r1b1kb1r/pp2qppp/5n2/2p5/2B1pp2/BPN5/P1P1QPPP/R3K2R b KQkq - bm Nd5; id "WAC.185";
rn1q1rk1/pp1bppbp/6p1/2pp4/3PP3/2N2N2/PP2BPPP/R1BQK2R w KQ - bm d5; id "WAC.186";
r2qkb1r/pb1n1ppp/1p2pn2/2ppN3/2PP4/2N3B1/PP2PPPP/R2QKB1R w KQkq - bm Nxf7; id "WAC.187";
r3r1k1/pp3ppp/2pb4/5pp1/2BP4/4BN2/PP3PPP/R3R1K1 b - - bm f4; id "WAC.188";
r2q1rk1/pp3ppp/3p1n2/4p3/1bB1P3/2N2N2/PPP2PPP/R1BQR1K1 b - - bm Bxc3+; id "WAC.189";
r1bqkb1r/pp2nppp/3p1n2/1Bp5/4P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - bm Bxf7+; id "WAC.190";
r3k2r/pppq1ppp/3bpn2/3p4/3P4/3BPN2/PPP1QPPP/R3K2R b KQkq - bm O-O-O; id "WAC.191";
r1b1k2r/pp3ppp/4pn2/3pN3/1b1P4/P1NB4/1PP2PPP/R1BQK2R b KQkq - bm Nd7; id "WAC.192";
rn1qkbnr/pp1bpppp/8/1BpP4/3p4/5N2/PPP2PPP/RNBQK2R w KQkq - bm Ne5; id "WAC.193";
r2qr1k1/pp1nbppp/2p2n2/3p4/3P4/2N1BN2/PPQ1PPPP/R3KB1R b KQ - bm Ne4; id "WAC.194";
r2q1rk1/ppbn1ppp/4p3/2pp1N2/3P4/2NB4/PPP2PPP/R1BQR1K1 w - - bm Nxg7; id "WAC.195";
r3k1r1/ppq2ppp/2pb1n2/1b6/3PP3/2NB1N2/PPP2PPP/R1BQ1RK1 b q - bm Bxe4; id "WAC.196";
r1b1kbnr/pppp1ppp/8/1B4q1/3PP3/5N2/PPP2PPP/RNBQK2R b KQkq - bm Qe7; id "WAC.197";
r3kb1r/pp3ppp/2p2n2/3q4/3P4/2N2N2/PPP2PPP/R1BQ1RK1 b kq - bm Qxd4; id "WAC.198";
r3r1k1/pbp2ppp/1p1q1n2/6B1/3P4/2N2N2/PPP1QPPP/4RRK1 b - - bm Ng4; id "WAC.199";
r2q1rk1/pp2bppp/1np2n2/3p4/3P4/P1NBPN2/1PP2PPP/R1BQR1K1 b - - bm Nfd7; id "WAC.200";
r1bqnrk1/ppp2p1p/3p2pB/4p3/3PP3/2N2N2/PPP2PPP/R2QK2R w KQ - bm Bxg7; id "WAC.201";
r3r1k1/pp3ppp/2pqbn2/3pb3/8/P1NBP3/1PP2PPP/R1BQR1K1 b - - bm Bxh2+; id "WAC.202";
r1bq1rk1/pp3ppp/2npbn2/4p3/2B1P3/2NP1N2/PPP2PPP/R1BQ1RK1 w - - bm d4; id "WAC.203";
r1bqr1k1/pp3ppp/2n2n2/3pp3/1bBPP3/2N2N2/PPP2PPP/R1BQR1K1 b - - bm Na5; id "WAC.204";
r1bqk2r/ppp1bppp/2n2n2/3pp3/4P3/3P1N2/PPP1BPPP/RNBQR1K1 b kq - bm O-O; id "WAC.205";
r1bq1rk1/pppp1ppp/2n2n2/4p3/2BPP3/8/PPP2PPP/RNBQK1NR b KQ - bm Na5; id "WAC.206";
r2qr1k1/pb1nbppp/1pn1p3/2ppP3/3P1B2/2NBBNN1/PPP2PPP/R2QK2R w KQ - bm Bxh7+; id "WAC.207";
r2q1rk1/ppp2ppp/3bpn2/3p4/3P4/P1NBPN2/1PP2PPP/R1BQR1K1 b - - bm Bg4; id "WAC.208";
3r1rk1/1ppb1ppp/p1q5/3p4/8/P1NB4/1PP2PPP/2RQR1K1 w - - bm Rxe8+; id "WAC.209";
r4rk1/ppp2ppp/3b1n2/q7/3PP3/2N2N2/PPP2PPP/R1BQR1K1 b - - bm Qxd4; id "WAC.210";
2r3k1/r4ppp/pp2pn2/8/3P4/4B3/PP3PPP/R3R1K1 b - - bm e5; id "WAC.211";
4r1k1/1ppq1ppp/p1nb1n2/4p3/2B1P3/P1NP1N2/1PP2PPP/R1BQR1K1 b - - bm Nd4; id "WAC.212";
r1bq1rk1/pp3ppp/1np1pn2/3p4/3P4/1BN1PN2/PPP2PPP/R1BQK2R b KQ - bm Ne4; id "WAC.213";
r3rbk1/pp2qppp/1npbb3/3p4/3P4/P1NBPN2/1PQ2PPP/R1B2RK1 b - - bm d4; id "WAC.214";
r2q1rk1/pp2bppp/2nppn2/4b3/2BPP3/2N2N2/PP3PPP/R1BQR1K1 w - - bm d5; id "WAC.215";
r4r1k/pp3ppp/3bbn2/q1pNp3/4P3/2NB4/PPP2PPP/R2QR1K1 w - - bm Nxb6; id "WAC.216";
r3r1k1/pp1n1ppp/2pb4/q7/3NP3/2NB4/PPP2PPP/R2QR1K1 w - - bm Nxc6; id "WAC.217";
r1bq1rk1/pp4pp/2n1pp2/3p4/3P4/2NBPN2/PPP2PPP/R1BQR1K1 b - - bm e5; id "WAC.218";
r2q1rk1/1bp1bppp/p3pn2/1p6/3P4/2N1PN2/PPQ1BPPP/R1B2RK1 b - - bm Nd5; id "WAC.219";
r1bq1rk1/pppp1ppp/5n2/2b5/2B1P3/8/PPP2PPP/RNBQK1NR w KQ - bm Bxf7+; id "WAC.220";
"""  # noqa: E501


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

_WAC_URL = (
    "https://raw.githubusercontent.com/fsmosca/chess-test-suite/master/epd/wac.epd"
)
_STS_URL = (
    "https://raw.githubusercontent.com/fsmosca/chess-test-suite/master/epd/STS1.epd"
)

KNOWN_SUITES = {
    "wac": _WAC_URL,
    "sts": _STS_URL,
    "builtin": None,
}


def download_epd(name: str, dest_path: str, timeout: int = 30) -> int:
    """
    Try to download a named suite ('wac' or 'sts') to dest_path.
    Falls back to the bundled copy if network is unavailable.
    Returns the number of bytes written.
    """
    urls = {
        "wac": [
            "https://raw.githubusercontent.com/niklasf/eco/master/epd/wac.epd",
            "https://raw.githubusercontent.com/official-stockfish/books/master/wac.epd",
        ],
        "sts": [
            "https://raw.githubusercontent.com/niklasf/eco/master/epd/STS1.epd",
        ],
    }
    candidates = urls.get(name.lower(), [])
    last_err = None
    for url in candidates:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = resp.read()
            with open(dest_path, "wb") as fh:
                fh.write(data)
            return len(data)
        except Exception as e:
            last_err = e

    # Fall back to bundled suite
    if name.lower() == "wac":
        text = WAC300_BUNDLED
        data = text.encode()
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return len(data)

    raise ValueError(f"Cannot download suite '{name}': {last_err}")


# ---------------------------------------------------------------------------
# Position tester
# ---------------------------------------------------------------------------

@dataclass
class PositionResult:
    position_id: str
    fen: str
    engine_move: str          # UCI
    best_moves_uci: List[str]
    avoid_moves_uci: List[str]
    correct: bool             # True = solved
    elapsed_ms: float
    score_cp: Optional[int] = None
    score_mate: Optional[int] = None
    depth: int = 0


@dataclass
class EpdResult:
    results: List[PositionResult] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def solved(self) -> int:
        return sum(1 for r in self.results if r.correct)

    @property
    def unsolved(self) -> int:
        return self.total - self.solved

    @property
    def accuracy(self) -> float:
        return self.solved / self.total if self.total else 0.0

    @property
    def avg_time_ms(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.elapsed_ms for r in self.results) / len(self.results)

    def summary(self) -> str:
        lines = [
            f"Positions : {self.total}",
            f"Solved    : {self.solved}  ({self.accuracy*100:.1f}%)",
            f"Unsolved  : {self.unsolved}",
            f"Avg time  : {self.avg_time_ms:.0f} ms/pos",
            f"Total time: {self.elapsed_s:.1f} s",
        ]
        return "\n".join(lines)


class EpdTester:
    """Run a list of EpdEntry positions through a UCIEngine."""

    def __init__(self, engine: UCIEngine):
        self._engine = engine

    def run(
        self,
        entries: List[EpdEntry],
        movetime_ms: int = 1000,
        depth: Optional[int] = None,
        verbose: bool = True,
        on_result: Optional[object] = None,
    ) -> EpdResult:
        result = EpdResult()
        t0 = time.perf_counter()

        for i, entry in enumerate(entries):
            fen = entry.fen
            self._engine.new_game()
            self._engine.position(fen, [])

            kwargs: dict = {}
            if depth is not None:
                kwargs["depth"] = depth
            if movetime_ms > 0:
                kwargs["movetime_ms"] = movetime_ms

            t_start = time.perf_counter()
            search = self._engine.go(**kwargs)
            elapsed = (time.perf_counter() - t_start) * 1000.0

            engine_move = search.bestmove or ""

            # Determine correctness
            if entry.best_moves:
                correct = engine_move in entry.best_moves
            elif entry.avoid_moves:
                correct = engine_move not in entry.avoid_moves and bool(engine_move)
            else:
                correct = bool(engine_move)  # no constraint → any move is fine

            pos_result = PositionResult(
                position_id=entry.id or f"pos{i+1}",
                fen=fen,
                engine_move=engine_move,
                best_moves_uci=entry.best_moves,
                avoid_moves_uci=entry.avoid_moves,
                correct=correct,
                elapsed_ms=elapsed,
                score_cp=search.score_cp,
                score_mate=search.score_mate,
                depth=search.depth or 0,
            )
            result.results.append(pos_result)

            if verbose:
                bm_str = ",".join(entry.best_moves) or "(none)"
                am_str = ",".join(entry.avoid_moves)
                tag = "[OK]" if correct else "[XX]"
                label = entry.id or f"pos{i+1}"
                print(
                    f"  {tag} {label:30s}  engine={engine_move:<7}  "
                    f"bm={bm_str:<12}  "
                    + (f"am={am_str}  " if am_str else "")
                    + f"d={pos_result.depth}  {elapsed:.0f}ms"
                )

            if on_result is not None:
                on_result(pos_result)

        result.elapsed_s = time.perf_counter() - t0
        return result
