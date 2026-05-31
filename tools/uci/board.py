"""
uci/board.py — Minimal pure-Python chess board tracker.

No external dependencies. Designed specifically for:
  • 50-move rule detection (half-move clock)
  • 3-fold repetition detection (position hashing)
  • Game-over detection (no legal moves reported by engine + mate/stalemate)
  • PGN coordinate conversion (UCI → SAN-ish for display)
  • FEN parsing for starting positions

NOT a complete chess engine. Does not generate moves. Does not validate
legality. Trusts the UCI engine for those responsibilities.

Board layout: 64 squares, a1=0, b1=1, ..., h8=63.
Pieces: uppercase = White, lowercase = Black.
  P/p = Pawn, N/n = Knight, B/b = Bishop, R/r = Rook, Q/q = Queen, K/k = King
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# ANSI colour constants and Unicode piece glyphs
# (Enable VT processing on Windows so escape codes work in PowerShell/cmd)
# ---------------------------------------------------------------------------
try:
    import ctypes as _ctypes
    _ctypes.windll.kernel32.SetConsoleMode(
        _ctypes.windll.kernel32.GetStdHandle(-11), 7
    )
except Exception:
    pass

_RESET     = "\033[0m"
_DIM       = "\033[2m"
_WHITE_SQ  = "\033[48;5;223m"   # light square bg
_BLACK_SQ  = "\033[48;5;130m"   # dark  square bg
_W_PIECE   = "\033[97m"         # bright white pieces
_B_PIECE   = "\033[30m"         # black pieces
_HL_SQ     = "\033[48;5;184m"   # highlighted square (last move)

_PIECE_UNICODE = {
    "P": "\u2659", "N": "\u2658", "B": "\u2657",
    "R": "\u2656", "Q": "\u2655", "K": "\u2654",
    "p": "\u265f", "n": "\u265e", "b": "\u265d",
    "r": "\u265c", "q": "\u265b", "k": "\u265a",
}

# Square helpers
def sq(file: int, rank: int) -> int:
    return rank * 8 + file

def file_of(s: int) -> int:  return s % 8
def rank_of(s: int) -> int:  return s // 8
def sq_name(s: int) -> str:  return "abcdefgh"[file_of(s)] + str(rank_of(s) + 1)
def parse_sq(name: str) -> int:
    return sq("abcdefgh".index(name[0]), int(name[1]) - 1)


# Starting position FEN
STARTPOS_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class MinimalBoard:
    """
    Tracks board state across a game for draw detection and move annotation.
    Initialise with a FEN (defaults to starting position) then call
    push_uci(move) after each move.
    """

    def __init__(self, fen: str = "startpos"):
        self.board: list[Optional[str]] = [None] * 64
        self.side: str = "w"                 # 'w' or 'b'
        self.castling: str = "KQkq"          # KQkq string
        self.ep_sq: Optional[int] = None     # en-passant target square index
        self.halfmove_clock: int = 0
        self.fullmove: int = 1

        # Position history for 3-fold repetition: maps key -> count
        self._pos_history: dict[tuple, int] = {}

        actual_fen = STARTPOS_FEN if fen in ("startpos", "") else fen
        self._parse_fen(actual_fen)
        self._record_position()

    # ------------------------------------------------------------------
    # FEN parsing
    # ------------------------------------------------------------------

    def _parse_fen(self, fen: str):
        parts = fen.split()
        if len(parts) < 2:
            raise ValueError(f"Invalid FEN: {fen!r}")

        # Piece placement
        ranks = parts[0].split("/")
        if len(ranks) != 8:
            raise ValueError(f"FEN piece placement invalid: {parts[0]!r}")
        for rank_idx, rank_str in enumerate(reversed(ranks)):  # rank 8 first in FEN
            file_idx = 0
            for ch in rank_str:
                if ch.isdigit():
                    file_idx += int(ch)
                else:
                    self.board[sq(file_idx, rank_idx)] = ch
                    file_idx += 1

        self.side     = parts[1] if len(parts) > 1 else "w"
        self.castling = parts[2] if len(parts) > 2 else "-"
        ep            = parts[3] if len(parts) > 3 else "-"
        self.ep_sq    = parse_sq(ep) if ep != "-" else None

        self.halfmove_clock = int(parts[4]) if len(parts) > 4 else 0
        self.fullmove       = int(parts[5]) if len(parts) > 5 else 1

    # ------------------------------------------------------------------
    # Move application
    # ------------------------------------------------------------------

    def push_uci(self, uci: str) -> dict:
        """
        Apply a UCI move and return a dict of move metadata:
          { 'from': sq, 'to': sq, 'piece': str, 'captured': str|None,
            'promotion': str|None, 'is_capture': bool, 'is_pawn': bool,
            'is_ep': bool, 'is_castle': bool, 'notation': str }
        """
        if len(uci) < 4:
            raise ValueError(f"Invalid UCI move: {uci!r}")

        from_sq = parse_sq(uci[0:2])
        to_sq   = parse_sq(uci[2:4])
        promo   = uci[4].upper() if len(uci) == 5 else None

        piece    = self.board[from_sq]
        captured = self.board[to_sq]

        # En-passant capture
        is_ep = (
            piece in ("P", "p") and
            self.ep_sq is not None and
            to_sq == self.ep_sq
        )
        if is_ep:
            # The captured pawn is one rank behind the to-square
            ep_cap_sq = to_sq - 8 if self.side == "w" else to_sq + 8
            captured = self.board[ep_cap_sq]
            self.board[ep_cap_sq] = None

        # Castling detection: king moves ≥ 2 files
        is_castle = (
            piece in ("K", "k") and
            abs(file_of(to_sq) - file_of(from_sq)) == 2
        )

        # Defensive: if a pawn reaches the back rank with no promo suffix
        # (engine bug / bare UCI), default to queen so the board stays legal.
        if piece in ("P", "p") and promo is None:
            target_rank = 7 if piece == "P" else 0
            if rank_of(to_sq) == target_rank:
                promo = "Q"

        # Compute notation NOW (before board is mutated) so disambiguation
        # and ray-blocking checks see the correct pre-move piece layout.
        notation = self._make_notation(uci, piece, captured, is_castle, promo, is_ep)

        # Promotion piece must match the moving side's colour.
        promo_board = None
        if promo:
            # piece can be None if the engine emitted a malformed/unknown move;
            # fall back to the current side to avoid AttributeError and keep
            # the board legal (assume white if side=='w').
            if piece:
                promo_board = promo if piece.isupper() else promo.lower()
            else:
                promo_board = promo if self.side == 'w' else promo.lower()

        # Apply basic move
        self.board[from_sq] = None
        self.board[to_sq]   = promo_board if promo_board else piece

        # Castling: move the rook too
        if is_castle:
            if to_sq > from_sq:  # kingside: king g, rook h→f
                rook_from = sq(7, rank_of(from_sq))
                rook_to   = sq(5, rank_of(from_sq))
            else:                # queenside: king c, rook a→d
                rook_from = sq(0, rank_of(from_sq))
                rook_to   = sq(3, rank_of(from_sq))
            rook = self.board[rook_from]
            self.board[rook_from] = None
            self.board[rook_to]   = rook

        # Update castling rights
        self._update_castling(from_sq, piece)

        # Update en-passant square
        if piece in ("P", "p") and abs(rank_of(to_sq) - rank_of(from_sq)) == 2:
            # Double pawn push — set ep square
            self.ep_sq = (from_sq + to_sq) // 2
        else:
            self.ep_sq = None

        # 50-move clock
        is_pawn = piece in ("P", "p")
        is_cap  = captured is not None
        if is_pawn or is_cap:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1

        # Full move counter
        if self.side == "b":
            self.fullmove += 1

        # Flip side
        self.side = "b" if self.side == "w" else "w"

        # Record position for repetition detection
        self._record_position()

        return {
            "from":      from_sq,
            "to":        to_sq,
            "piece":     piece,
            "captured":  captured,
            "promotion": promo,
            "is_capture": is_cap or is_ep,
            "is_pawn":   is_pawn,
            "is_ep":     is_ep,
            "is_castle": is_castle,
            "notation":  notation,
        }

    def _update_castling(self, from_sq: int, piece: Optional[str]):
        if not piece:
            return
        rights = set(self.castling) - {"-"}
        # Moving king removes both castling rights for that side
        if piece == "K": rights -= {"K", "Q"}
        if piece == "k": rights -= {"k", "q"}
        # Moving rook from corner removes that side's castling right
        if piece == "R":
            if from_sq == sq(0, 0): rights.discard("Q")
            if from_sq == sq(7, 0): rights.discard("K")
        if piece == "r":
            if from_sq == sq(0, 7): rights.discard("q")
            if from_sq == sq(7, 7): rights.discard("k")
        self.castling = "".join(
            c for c in "KQkq" if c in rights
        ) or "-"

    def _position_key(self) -> tuple:
        """Hashable representation of the position for 3-fold detection."""
        return (
            tuple(self.board),
            self.side,
            self.castling,
            self.ep_sq,
        )

    def _record_position(self):
        key = self._position_key()
        self._pos_history[key] = self._pos_history.get(key, 0) + 1

    @property
    def is_threefold_repetition(self) -> bool:
        return any(v >= 3 for v in self._pos_history.values())

    @property
    def is_fifty_move_rule(self) -> bool:
        return self.halfmove_clock >= 100  # 100 half-moves = 50 full moves

    @property
    def is_insufficient_material(self) -> bool:
        """Returns True for KvK, KvKB, KvKN (simple check only)."""
        pieces = [p for p in self.board if p is not None]
        if len(pieces) > 4:
            return False
        types = set(p.upper() for p in pieces)
        if types == {"K"}:
            return True
        if types == {"K", "N"} or types == {"K", "B"}:
            return True
        return False

    # ------------------------------------------------------------------
    # Display / notation helpers
    # ------------------------------------------------------------------

    def _disambig(self, piece: str, from_sq: int, to_sq: int) -> str:
        """
        Return the minimal SAN disambiguation string (file, rank, or full square)
        for a non-pawn move.  Must be called BEFORE the move is applied so that
        self.board still reflects the pre-move layout for ray-blocking checks.
        """
        # Collect other pieces of the same type+colour that can also reach to_sq
        ambiguous = [
            s for s in range(64)
            if s != from_sq
            and self.board[s] == piece
            and self._can_reach(piece.upper(), s, to_sq)
        ]
        if not ambiguous:
            return ""
        ff = file_of(from_sq)
        fr = rank_of(from_sq)
        # Prefer file disambiguation if all conflicting pieces are on different files
        if all(file_of(s) != ff for s in ambiguous):
            return "abcdefgh"[ff]
        # Otherwise use rank disambiguation if unique on that rank
        if all(rank_of(s) != fr for s in ambiguous):
            return str(fr + 1)
        # Fall back to full square
        return sq_name(from_sq)

    def _make_notation(
        self,
        uci:      str,
        piece:    Optional[str],
        captured: Optional[str],
        castle:   bool,
        promo:    Optional[str],
        is_ep:    bool,
    ) -> str:
        """
        Build SAN-style notation for display.  Must be called BEFORE the move is
        applied to the board so disambiguation and ray checks are correct.
        No check/mate symbols (+/#) — those require full move generation.
        """
        if castle:
            from_sq = parse_sq(uci[0:2])
            to_sq   = parse_sq(uci[2:4])
            return "O-O-O" if to_sq < from_sq else "O-O"
        if not piece:
            return uci
        p    = piece.upper()
        dest = uci[2:4]
        cap  = "x" if (captured or is_ep) else ""
        if p == "P":
            if captured or is_ep:
                note = uci[0] + "x" + dest
            else:
                note = dest
            if promo:
                note += "=" + promo.upper()
        else:
            from_sq  = parse_sq(uci[0:2])
            to_sq    = parse_sq(uci[2:4])
            disambig = self._disambig(piece, from_sq, to_sq)
            note     = p + disambig + cap + dest
        return note

    def ascii(self) -> str:
        """Return an ASCII representation of the current board."""
        rows = []
        rows.append("  +---+---+---+---+---+---+---+---+")
        for r in range(7, -1, -1):
            row = f"{r+1} |"
            for f in range(8):
                p = self.board[sq(f, r)]
                row += f" {p if p else '.'} |"
            rows.append(row)
            rows.append("  +---+---+---+---+---+---+---+---+")
        rows.append("    a   b   c   d   e   f   g   h")
        return "\n".join(rows)

    # ------------------------------------------------------------------
    # SAN → UCI conversion
    # ------------------------------------------------------------------

    def san_to_uci(self, san: str) -> Optional[str]:
        """
        Convert a SAN move string to a UCI move string for the current position.
        Returns None if the move cannot be resolved.

        Handles: piece moves, pawn moves, captures, promotions (e8=Q / e8Q),
        en-passant, and castling (O-O, O-O-O, 0-0, 0-0-0).
        Strips check (+), mate (#), and annotation (!, ?) suffixes.
        """
        import re
        s = san.strip().rstrip('+#!?').strip()
        if not s:
            return None

        # Castling — king moves two squares along its rank
        if s in ("O-O", "0-0"):
            king = "K" if self.side == "w" else "k"
            for i, p in enumerate(self.board):
                if p == king:
                    return sq_name(i) + sq_name(i + 2)
            return None
        if s in ("O-O-O", "0-0-0"):
            king = "K" if self.side == "w" else "k"
            for i, p in enumerate(self.board):
                if p == king:
                    return sq_name(i) + sq_name(i - 2)
            return None

        # Promotion suffix: e8=Q  or  dxc8R  or  e8Q
        promo: Optional[str] = None
        promo_match = re.search(r"=?([QqRrBbNn])$", s)
        if promo_match:
            promo = promo_match.group(1).lower()
            s = s[: promo_match.start()]

        # Destination square is always the last two chars
        if len(s) < 2:
            return None
        dest_name = s[-2:]
        try:
            dest = parse_sq(dest_name)
        except (ValueError, IndexError):
            return None
        rest = s[:-2].replace("x", "")  # strip capture indicator

        # Determine piece type and disambiguation string
        if rest and rest[0].isupper() and rest[0] in "NBRQK":
            piece_type = rest[0]   # N B R Q K
            disambig   = rest[1:]
        else:
            piece_type = "P"       # pawn
            disambig   = rest      # will be the source file if any

        target_piece = piece_type if self.side == "w" else piece_type.lower()

        # Gather candidate from-squares
        candidates: list[int] = []
        for from_sq in range(64):
            if self.board[from_sq] != target_piece:
                continue
            if self._can_reach(piece_type, from_sq, dest):
                candidates.append(from_sq)

        # Apply disambiguation
        if disambig:
            if len(disambig) == 2:
                # Full source square (e.g. "Qd1e2" → disambig="d1")
                try:
                    src = parse_sq(disambig)
                    candidates = [c for c in candidates if c == src]
                except (ValueError, IndexError):
                    pass
            elif disambig[0].isalpha():
                f = "abcdefgh".find(disambig[0])
                if f >= 0:
                    candidates = [c for c in candidates if file_of(c) == f]
            elif disambig[0].isdigit():
                r = int(disambig[0]) - 1
                candidates = [c for c in candidates if rank_of(c) == r]

        if len(candidates) != 1:
            return None

        uci = sq_name(candidates[0]) + dest_name
        if promo:
            uci += promo
        return uci

    def _can_reach(self, piece_type: str, from_sq: int, to_sq: int) -> bool:
        """
        Return True if a piece of piece_type on from_sq can pseudo-legally
        move to to_sq (does not validate check, but respects board occupancy).
        """
        us   = self.side
        them = "b" if us == "w" else "w"
        tgt  = self.board[to_sq]

        # Cannot capture own piece
        if tgt:
            if us == "w" and tgt.isupper():
                return False
            if us == "b" and tgt.islower():
                return False

        fr = rank_of(from_sq); ff = file_of(from_sq)
        tr = rank_of(to_sq);   tf = file_of(to_sq)
        dr = tr - fr;           dc = tf - ff

        if piece_type == "P":
            direction  = 1 if us == "w" else -1
            start_rank = 1 if us == "w" else 6
            if dc == 0:
                if tgt:
                    return False
                if dr == direction:
                    return True
                if dr == 2 * direction and fr == start_rank:
                    mid = sq(ff, fr + direction)
                    return self.board[mid] is None
            elif abs(dc) == 1 and dr == direction:
                if tgt:
                    return True                # normal diagonal capture
                if self.ep_sq == to_sq:
                    return True                # en-passant
            return False

        elif piece_type == "N":
            return (abs(dr), abs(dc)) in {(1, 2), (2, 1)}

        elif piece_type == "B":
            if abs(dr) != abs(dc) or dr == 0:
                return False
            return self._ray_clear(from_sq, to_sq)

        elif piece_type == "R":
            if dr != 0 and dc != 0:
                return False
            if dr == 0 and dc == 0:
                return False
            return self._ray_clear(from_sq, to_sq)

        elif piece_type == "Q":
            if dr == 0 and dc == 0:
                return False
            if not (abs(dr) == abs(dc) or dr == 0 or dc == 0):
                return False
            return self._ray_clear(from_sq, to_sq)

        elif piece_type == "K":
            return abs(dr) <= 1 and abs(dc) <= 1 and (dr != 0 or dc != 0)

        return False

    def _ray_clear(self, from_sq: int, to_sq: int) -> bool:
        """True if all squares between from_sq and to_sq (exclusive) are empty."""
        fr = rank_of(from_sq); ff = file_of(from_sq)
        tr = rank_of(to_sq);   tf = file_of(to_sq)
        dr = 0 if tr == fr else (1 if tr > fr else -1)
        dc = 0 if tf == ff else (1 if tf > ff else -1)
        r, c = fr + dr, ff + dc
        while (r, c) != (tr, tf):
            if self.board[sq(c, r)] is not None:
                return False
            r += dr; c += dc
        return True

    def fen(self) -> str:
        """Return the FEN string for the current position."""
        rows = []
        for r in range(7, -1, -1):
            empty = 0
            row = ""
            for f in range(8):
                p = self.board[sq(f, r)]
                if p:
                    if empty:
                        row += str(empty); empty = 0
                    row += p
                else:
                    empty += 1
            if empty:
                row += str(empty)
            rows.append(row)
        ep = sq_name(self.ep_sq) if self.ep_sq is not None else "-"
        return (f"{'/'.join(rows)} {self.side} {self.castling} {ep} "
                f"{self.halfmove_clock} {self.fullmove}")
    def render_ansi(
        self,
        flipped: bool = False,
        last_move: tuple[int, int] | None = None,
    ) -> str:
        """Return a coloured ANSI + Unicode board string for terminal display."""
        return render_board(self, flipped=flipped, last_move=last_move)


# ---------------------------------------------------------------------------
# Standalone ANSI board renderer (usable without MinimalBoard subclassing)
# ---------------------------------------------------------------------------

def render_board(
    board: "MinimalBoard",
    flipped: bool = False,
    last_move: tuple[int, int] | None = None,
    player_side: str = "w",
) -> str:
    """Render a MinimalBoard as a coloured ANSI + Unicode string."""
    lines = []
    highlight = set(last_move) if last_move else set()
    ranks = range(7, -1, -1) if not flipped else range(0, 8)
    files = range(0, 8)      if not flipped else range(7, -1, -1)
    sep = f"  {_DIM}+---+---+---+---+---+---+---+---+{_RESET}"
    lines.append(sep)
    for r in ranks:
        row = f"{_DIM}{r+1}{_RESET} {_DIM}|{_RESET}"
        for f in files:
            s = sq(f, r)
            piece    = board.board[s]
            is_light = (f + r) % 2 == 1
            in_hl    = s in highlight
            if in_hl:
                bg = _HL_SQ
            elif is_light:
                bg = _WHITE_SQ
            else:
                bg = _BLACK_SQ
            if piece:
                glyph = _PIECE_UNICODE.get(piece, piece)
                fg = _W_PIECE if piece.isupper() else _B_PIECE
                row += f"{bg}{fg} {glyph} {_RESET}"
            else:
                row += f"{bg}   {_RESET}"
            row += f"{_DIM}|{_RESET}"
        lines.append(row)
        lines.append(sep)
    file_labels = "abcdefgh" if not flipped else "hgfedcba"
    lines.append("   " + "  ".join(
        f"{_DIM}{c}{_RESET}" for c in file_labels
    ) + "   ")
    return "\n".join(lines)