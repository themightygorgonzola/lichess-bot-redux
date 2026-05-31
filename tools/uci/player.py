"""
uci/player.py — Abstract Player interface and built-in implementations.

To integrate with an external API (Lichess, custom server, human input, etc.),
subclass Player and implement get_move(). The rest of the framework
(GameSession, MatchRunner) works unchanged.

Example — minimal Lichess adapter stub:

    class LichessPlayer(Player):
        def __init__(self, game_id, session):
            self._game_id = game_id
            self._session = session

        @property
        def label(self): return "LichessOpponent"

        def get_move(self, fen, moves, tc, side):
            # Poll the Lichess game-stream API for the opponent's move
            ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import UCIEngine, SearchResult


# ---------------------------------------------------------------------------
# TimeControl — passed to Player.get_move() every turn
# ---------------------------------------------------------------------------

@dataclass
class TimeControl:
    """
    Flexible time control descriptor.

    Exactly one of movetime_ms or (wtime/btime) should be set.
    When using clock-based control, the GameSession keeps running totals
    and passes the current remaining times on each turn.
    """
    movetime_ms: Optional[int] = None   # Fixed ms per move (ignores clock)
    wtime:       int           = 0      # White's remaining time (ms)
    btime:       int           = 0      # Black's remaining time (ms)
    winc:        int           = 0      # White increment (ms)
    binc:        int           = 0      # Black increment (ms)
    movestogo:   int           = 0      # Moves until next time control (0 = sudden death)
    depth:       Optional[int] = None   # Fixed depth (overrides time)

    def for_side(self, side: str) -> dict:
        """
        Return the go() kwargs appropriate for the given side ('white'/'black').
        Drops None values so they are not passed to UCIEngine.go().
        """
        kwargs: dict = {}
        if self.depth is not None:
            kwargs["depth"] = self.depth
        elif self.movetime_ms is not None:
            kwargs["movetime_ms"] = self.movetime_ms
        else:
            kwargs["wtime"] = self.wtime
            kwargs["btime"] = self.btime
            kwargs["winc"]  = self.winc
            kwargs["binc"]  = self.binc
            if self.movestogo:
                kwargs["movestogo"] = self.movestogo
        return kwargs

    def clone_with(self, **overrides) -> "TimeControl":
        from dataclasses import replace
        return replace(self, **overrides)


# ---------------------------------------------------------------------------
# Player — abstract base
# ---------------------------------------------------------------------------

class Player(ABC):
    """
    Abstract player interface.  Implement this to plug any move source
    (UCI engine, Lichess API, human CLI input, neural net, etc.) into
    GameSession / MatchRunner.
    """

    @property
    @abstractmethod
    def label(self) -> str:
        """Human-readable name shown in PGN and output."""
        ...

    @abstractmethod
    def get_move(
        self,
        fen:   str,
        moves: list[str],
        tc:    TimeControl,
        side:  str,         # "white" or "black"
    ) -> str:
        """
        Given the current position (FEN + move list) and time control,
        return a legal UCI move string (e.g. "e2e4", "e7e8q").
        Return "0000" or "(none)" to resign / signal no legal move.
        """
        ...

    def new_game(self):
        """Called before each game starts. Override to reset state."""

    def game_over(self, result: str):
        """Called after game ends. result is e.g. '1-0', '0-1', '1/2-1/2'."""

    def close(self):
        """Called when the player is no longer needed. Release resources."""


# ---------------------------------------------------------------------------
# UCIPlayer — wraps UCIEngine as a Player
# ---------------------------------------------------------------------------

class UCIPlayer(Player):
    """
    A Player backed by a UCIEngine subprocess.

    The engine is started externally and passed in; this allows the same
    engine instance to be reused across games or replaced between games.
    """

    def __init__(self, engine: "UCIEngine", label: Optional[str] = None):
        self._engine = engine
        self._label  = label or engine.name

    @property
    def label(self) -> str:
        return self._label

    @property
    def engine(self) -> "UCIEngine":
        return self._engine

    def new_game(self):
        self._engine.new_game()

    def get_move(
        self,
        fen:   str,
        moves: list[str],
        tc:    TimeControl,
        side:  str,
    ) -> str:
        self._engine.position(fen, moves)
        result = self._engine.go(**tc.for_side(side))
        return result.bestmove or "0000"

    def get_move_result(
        self,
        fen:   str,
        moves: list[str],
        tc:    TimeControl,
        side:  str,
    ) -> "SearchResult":
        """Like get_move() but returns the full SearchResult."""
        self._engine.position(fen, moves)
        return self._engine.go(**tc.for_side(side))

    def close(self):
        self._engine.close()
