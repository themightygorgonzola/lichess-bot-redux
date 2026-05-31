"""
uci/game.py — Single game session between two Players.

GameSession drives one chess game from start to finish:
  1. Alternates between the two Players calling get_move()
  2. Applies each move to a MinimalBoard for draw-rule tracking
  3. Detects game-over: null move from engine, 50-move, 3-fold, insufficient material
  4. Optionally calls event callbacks for live display / logging
  5. Returns a GameResult with the full move list and outcome

Usage:
    from uci import UCIEngine, UCIPlayer, TimeControl, GameSession

    p1 = UCIPlayer(UCIEngine("build/lichess-bot.exe", threads=4), label="Engine-A")
    p2 = UCIPlayer(UCIEngine("build/lichess-bot.exe", threads=1), label="Engine-B")
    tc = TimeControl(movetime_ms=500)

    session = GameSession(p1, p2, tc, starting_fen="startpos")
    result  = session.play(on_move=print)
    print(result.pgn())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional, TYPE_CHECKING

from .board  import MinimalBoard
from .player import Player, TimeControl

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# GameStatus / GameResult
# ---------------------------------------------------------------------------

class GameStatus(Enum):
    IN_PROGRESS          = auto()
    CHECKMATE            = auto()
    STALEMATE            = auto()
    DRAW_50_MOVE         = auto()
    DRAW_REPETITION      = auto()
    DRAW_INSUFFICIENT    = auto()
    DRAW_AGREEMENT       = auto()    # future: players can agree
    RESIGN               = auto()
    TIMEOUT              = auto()
    ERROR                = auto()


@dataclass
class MoveRecord:
    uci:        str
    notation:   str
    side:       str          # "white" or "black"
    score_cp:   Optional[int] = None
    score_mate: Optional[int] = None
    depth:      int = 0
    nps:        int = 0
    time_ms:    int = 0
    pv:         str = ""     # principal variation (space-separated UCI moves)


@dataclass
class GameResult:
    white_label:  str
    black_label:  str
    status:       GameStatus
    winner:       Optional[str]   = None  # "white", "black", or None (draw)
    moves:        list[MoveRecord] = field(default_factory=list)
    starting_fen: str              = "startpos"
    final_fen:    str              = ""          # FEN after the last move
    elapsed_s:    float            = 0.0

    @property
    def score(self) -> str:
        """PGN result string: '1-0', '0-1', '1/2-1/2', or '*'."""
        if self.winner == "white":   return "1-0"
        if self.winner == "black":   return "0-1"
        if self.status != GameStatus.IN_PROGRESS: return "1/2-1/2"
        return "*"

    @property
    def termination(self) -> str:
        return {
            GameStatus.CHECKMATE:          "checkmate",
            GameStatus.STALEMATE:          "stalemate",
            GameStatus.DRAW_50_MOVE:       "50-move rule",
            GameStatus.DRAW_REPETITION:    "threefold repetition",
            GameStatus.DRAW_INSUFFICIENT:  "insufficient material",
            GameStatus.DRAW_AGREEMENT:     "draw agreement",
            GameStatus.RESIGN:             "resignation",
            GameStatus.TIMEOUT:            "timeout",
            GameStatus.ERROR:              "error",
            GameStatus.IN_PROGRESS:        "in progress",
        }.get(self.status, "unknown")

    def pgn(self, event: str = "Self-play", date: str = "????.??.??") -> str:
        """Return a PGN string for this game."""
        lines = [
            f'[Event "{event}"]',
            f'[Date "{date}"]',
            f'[White "{self.white_label}"]',
            f'[Black "{self.black_label}"]',
            f'[Result "{self.score}"]',
            f'[Termination "{self.termination}"]',
        ]
        if self.starting_fen not in ("startpos", ""):
            lines.append(f'[FEN "{self.starting_fen}"]')
            lines.append('[SetUp "1"]')
        lines.append("")  # blank line before moves

        # Build move text
        tokens: list[str] = []
        for i, m in enumerate(self.moves):
            if m.side == "white":
                tokens.append(f"{(i // 2) + 1}.")
            # Annotation with score
            annot = m.notation
            if m.score_cp is not None:
                annot += f" {{{'+' if m.score_cp >= 0 else ''}{m.score_cp / 100:.2f}}}"
            elif m.score_mate is not None:
                annot += f" {{M{m.score_mate}}}"
            tokens.append(annot)
        tokens.append(self.score)

        # Wrap to ~80 cols
        current_line = ""
        for t in tokens:
            if len(current_line) + len(t) + 1 > 79:
                lines.append(current_line.rstrip())
                current_line = t + " "
            else:
                current_line += t + " "
        if current_line.strip():
            lines.append(current_line.rstrip())

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# MoveEvent — passed to on_move callback
# ---------------------------------------------------------------------------

@dataclass
class MoveEvent:
    move_num:    int        # Full-move number (1-based)
    side:        str        # "white" or "black"
    record:      MoveRecord
    board:       MinimalBoard
    uci_moves:   list[str]  # All UCI moves so far


# ---------------------------------------------------------------------------
# GameSession
# ---------------------------------------------------------------------------

class GameSession:
    """
    Drives a single game between two Players.

    Players can be any subclass — UCIPlayer, LichessPlayer, HumanPlayer, etc.
    The session is completely synchronous; call play() to block until the game
    ends, receiving optional callbacks at each move.
    """

    # Sanity cap to prevent infinite games on buggy engines
    MAX_MOVES = 500

    def __init__(
        self,
        white:        Player,
        black:        Player,
        tc:           TimeControl,
        starting_fen: str = "startpos",
    ):
        self.white        = white
        self.black        = black
        self.tc           = tc
        self.starting_fen = starting_fen

    def play(
        self,
        on_move: Optional[Callable[[MoveEvent], None]] = None,
        on_result: Optional[Callable[[GameResult], None]] = None,
        annotate: bool = True,   # Record score/depth in MoveRecord (UCIPlayer only)
    ) -> GameResult:
        """
        Run the game to completion. Thread-blocking.

        on_move(MoveEvent)     — called after each legal move
        on_result(GameResult)  — called once when game ends
        annotate               — if True, capture search info for PGN annotation

        Returns GameResult.
        """
        from .player import UCIPlayer  # avoid circular at module level

        board = MinimalBoard(self.starting_fen)
        uci_moves: list[str] = []
        move_records: list[MoveRecord] = []

        t0 = time.time()

        # Clone the TC so we can track remaining clocks per side
        tc = self.tc

        self.white.new_game()
        self.black.new_game()

        result = GameResult(
            white_label  = self.white.label,
            black_label  = self.black.label,
            status       = GameStatus.IN_PROGRESS,
            starting_fen = self.starting_fen,
        )

        for total_half_moves in range(self.MAX_MOVES):
            side_str   = "white" if board.side == "w" else "black"
            player     = self.white if side_str == "white" else self.black
            move_num   = board.fullmove

            # --- Passive draw checks (before asking for a move) ---
            if board.is_fifty_move_rule:
                result.status = GameStatus.DRAW_50_MOVE
                break
            if board.is_threefold_repetition:
                result.status = GameStatus.DRAW_REPETITION
                break
            if board.is_insufficient_material:
                result.status = GameStatus.DRAW_INSUFFICIENT
                break

            # --- Get move from player ---
            move_t0 = time.time()
            if isinstance(player, UCIPlayer):
                # Always fetch SearchResult from UCIPlayer — we need bestmove
                # AND score_mate for correct checkmate vs stalemate detection.
                # annotate flag only controls whether we write score/depth into
                # the MoveRecord (PGN annotation); it must not suppress sr.
                sr  = player.get_move_result(self.starting_fen, uci_moves, tc, side_str)
                uci = sr.bestmove or "0000"
            else:
                uci = player.get_move(self.starting_fen, uci_moves, tc, side_str)
                sr  = None
            move_elapsed_ms = int((time.time() - move_t0) * 1000)

            # --- Detect no legal moves (checkmate or stalemate) ---
            if not uci or uci in ("0000", "(none)"):
                # Engine reports no legal move — figure out checkmate vs stalemate
                # by looking at the last score: if it's not a mate score, engine
                # must have found stalemate. We conservatively check via null-move
                # detection. This path is rare; just trust the engine.
                result.status = GameStatus.STALEMATE
                # Checkmate: the side that just moved wins
                if sr and sr.score_mate is not None:
                    result.status = GameStatus.CHECKMATE
                    opp = "black" if side_str == "white" else "white"
                    result.winner = opp
                else:
                    # No mate score — treat as stalemate (draw)
                    result.status = GameStatus.STALEMATE
                break

            # --- Apply move to board ---
            meta = board.push_uci(uci)
            uci_moves.append(uci)

            rec = MoveRecord(
                uci      = uci,
                notation = meta["notation"],
                side     = side_str,
                time_ms  = move_elapsed_ms,
            )
            if sr and annotate:
                rec.score_cp   = sr.score_cp
                rec.score_mate = sr.score_mate
                rec.depth      = sr.depth
                rec.nps        = sr.nps
                rec.time_ms    = sr.time_ms or move_elapsed_ms
                rec.pv         = sr.pv or ""
            move_records.append(rec)

            if on_move:
                on_move(MoveEvent(move_num, side_str, rec, board, list(uci_moves)))

        result.moves     = move_records
        result.final_fen = board.fen()
        result.elapsed_s = time.time() - t0

        # If we hit the move cap without a terminal
        if result.status == GameStatus.IN_PROGRESS:
            result.status = GameStatus.DRAW_50_MOVE  # treat as draw after cap

        self.white.game_over(result.score)
        self.black.game_over(result.score)

        if on_result:
            on_result(result)

        return result
