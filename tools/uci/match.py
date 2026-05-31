"""
uci/match.py — Multi-game match runner between two Players.

Runs a configurable number of games, alternates colors every game,
accumulates results, prints live progress, and optionally writes a PGN file.

Usage:
    from uci import UCIEngine, UCIPlayer, TimeControl, MatchRunner, MatchConfig

    p1 = UCIPlayer(UCIEngine("build/lichess-bot.exe", threads=8), label="Engine-A")
    p2 = UCIPlayer(UCIEngine("build/lichess-bot.exe", threads=1), label="Engine-B")
    tc = TimeControl(movetime_ms=200)

    config = MatchConfig(games=20, tc=tc, swap_colors=True, pgn_path="match.pgn")
    result = MatchRunner(p1, p2, config).run()
    print(result.summary())
"""

from __future__ import annotations

import datetime
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from .player  import Player, TimeControl
from .game    import GameSession, GameResult, GameStatus, MoveEvent
from .board   import render_board as _render_board


# ---------------------------------------------------------------------------
# MatchConfig
# ---------------------------------------------------------------------------

@dataclass
class MatchConfig:
    games:         int                    = 2
    tc:            TimeControl            = field(default_factory=lambda: TimeControl(movetime_ms=500))
    swap_colors:   bool                   = True    # Alternate colors every game
    starting_fen:  str                    = "startpos"
    pgn_path:      Optional[str]          = None    # Write all games to this file
    verbose:       bool                   = True    # Print live progress
    verbose_board: bool                   = False   # Print ASCII board + FEN + PV after each move
    annotate:      bool                   = True    # Include score annotations in PGN
    event_name:    str                    = "Self-play match"
    # Hook: called after each game (beyond the built-in reporting)
    on_game_end:   Optional[Callable[[GameResult, int], None]] = field(
        default=None, compare=False, repr=False
    )


# ---------------------------------------------------------------------------
# MatchResult
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    player1_label: str
    player2_label: str
    games:         list[GameResult]  = field(default_factory=list)
    elapsed_s:     float             = 0.0

    # Scores from player1's perspective
    @property
    def wins(self) -> int:
        return sum(1 for g in self.signed_games if g[1] == 1.0)

    @property
    def losses(self) -> int:
        return sum(1 for g in self.signed_games if g[1] == 0.0)

    @property
    def draws(self) -> int:
        return sum(1 for g in self.signed_games if g[1] == 0.5)

    @property
    def score(self) -> float:
        return sum(s for _, s in self.signed_games)

    @property
    def signed_games(self) -> list[tuple[GameResult, float]]:
        """Returns (game, p1_score) for each game. p1_score in {0, 0.5, 1}."""
        result = []
        for i, g in enumerate(self.games):
            p1_color = "white" if i % 2 == 0 else "black"
            if g.winner is None:
                result.append((g, 0.5))
            elif g.winner == p1_color:
                result.append((g, 1.0))
            else:
                result.append((g, 0.0))
        return result

    @property
    def elo_diff(self) -> Optional[float]:
        """
        Elo difference estimate: positive means player1 is stronger.
        Returns None when the sample is too small or score is 0 or n.
        Formula: -400 * log10(1/score_rate - 1)
        """
        n = len(self.games)
        if n == 0:
            return None
        s = self.score
        # Clamp to avoid log(0)
        if s <= 0 or s >= n:
            return None
        score_rate = s / n
        return -400.0 * math.log10(1.0 / score_rate - 1.0)

    @property
    def los(self) -> float:
        """
        Likelihood of Superiority (0–1): probability player1 is stronger.
        Uses normal approximation over win/draw/loss counts.
        0.95+ is conventionally considered significant.
        """
        w = self.wins
        d = self.draws
        l_ = self.losses
        n = w + d + l_
        if n == 0:
            return 0.5
        # Expected score variance under H0 (p=0.5 per game)
        # var = (w + d/4) is a common approximation; full form below
        mu  = (w + 0.5 * d) / n      # observed score fraction
        # Wald std-error for binomial proportion
        se  = math.sqrt(mu * (1.0 - mu) / n)
        if se == 0.0:
            return 1.0 if mu > 0.5 else 0.0
        z = (mu - 0.5) / se
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    def summary(self) -> str:
        n = len(self.games)
        pct = (self.score / n * 100) if n else 0
        lines = [
            "",
            "=" * 60,
            f"  Match Result: {self.player1_label} vs {self.player2_label}",
            "=" * 60,
            f"  Games played : {n}",
            f"  {self.player1_label:20s}: +{self.wins}  ={self.draws}  -{self.losses}  "
            f"({self.score:.1f}/{n},  {pct:.1f}%)",
            f"  {self.player2_label:20s}: +{self.losses}  ={self.draws}  -{self.wins}  "
            f"({n - self.score:.1f}/{n},  {100 - pct:.1f}%)",
            f"  Elapsed       : {self.elapsed_s:.1f}s",
        ]
        elo = self.elo_diff
        if elo is not None:
            lines.append(f"  Elo diff      : {elo:+.1f} (p1 relative to p2)")
        lines.append(f"  LOS           : {self.los*100:.1f}%")
        # Termination breakdown
        from collections import Counter
        terms = Counter(g.termination for g in self.games)
        if terms:
            lines.append("  Terminations  :")
            for t, c in sorted(terms.items()):
                lines.append(f"    {t}: {c}")
        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# MatchRunner
# ---------------------------------------------------------------------------

class MatchRunner:
    """
    Runs a MatchConfig-defined match between two Players.

    Colors are optionally swapped every game (swap_colors=True) to reduce
    first-move advantage bias. Player1 always plays white in even-indexed
    games (0, 2, 4...) and black in odd-indexed games.
    """

    def __init__(self, player1: Player, player2: Player, config: MatchConfig):
        self.player1 = player1
        self.player2 = player2
        self.config  = config

    def run(self) -> MatchResult:
        cfg     = self.config
        result  = MatchResult(
            player1_label = self.player1.label,
            player2_label = self.player2.label,
        )
        t0 = time.time()
        today = datetime.date.today().strftime("%Y.%m.%d")

        pgn_file = None
        if cfg.pgn_path:
            pgn_file = open(cfg.pgn_path, "w", encoding="utf-8")

        if cfg.verbose:
            print(f"\n{'='*60}")
            print(f"  {cfg.event_name}")
            print(f"  {self.player1.label}  vs  {self.player2.label}")
            tc_desc = (
                f"movetime {cfg.tc.movetime_ms}ms"
                if cfg.tc.movetime_ms else
                f"wtime {cfg.tc.wtime}ms / btime {cfg.tc.btime}ms"
            )
            print(f"  Time control  : {tc_desc}")
            print(f"  Games         : {cfg.games}")
            print(f"{'='*60}\n")

        try:
            for game_idx in range(cfg.games):
                # Assign colors
                if cfg.swap_colors and game_idx % 2 == 1:
                    white, black = self.player2, self.player1
                    p1_color = "black"
                else:
                    white, black = self.player1, self.player2
                    p1_color = "white"

                if cfg.verbose:
                    print(f"  Game {game_idx + 1}/{cfg.games}  "
                          f"White: {white.label}  Black: {black.label}")

                session = GameSession(
                    white        = white,
                    black        = black,
                    tc           = cfg.tc,
                    starting_fen = cfg.starting_fen,
                )

                on_move = self._make_move_callback(cfg.verbose, game_idx)
                game_result = session.play(on_move=on_move, annotate=cfg.annotate)
                result.games.append(game_result)

                # Score line
                if cfg.verbose:
                    p1_score = self._p1_score(game_result, p1_color)
                    p1_total = sum(s for _, s in result.signed_games)
                    n_played = game_idx + 1
                    print(
                        f"\r  Result: {game_result.score}  "
                        f"({game_result.termination})  "
                        f"Moves: {len(game_result.moves)}  "
                        f"Time: {game_result.elapsed_s:.1f}s"
                    )
                    print(
                        f"  Score: {self.player1.label} "
                        f"{p1_total:.1f}/{n_played}\n"
                    )

                # Write PGN
                if pgn_file:
                    pgn_text = game_result.pgn(
                        event=cfg.event_name,
                        date=today,
                    )
                    pgn_file.write(pgn_text + "\n\n")
                    pgn_file.flush()

                if cfg.on_game_end:
                    cfg.on_game_end(game_result, game_idx)

        finally:
            if pgn_file:
                pgn_file.close()

        result.elapsed_s = time.time() - t0

        if cfg.verbose:
            print(result.summary())

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _p1_score(self, game: GameResult, p1_color: str) -> float:
        if game.winner is None:
            return 0.5
        return 1.0 if game.winner == p1_color else 0.0

    def _make_move_callback(
        self, verbose: bool, game_idx: int
    ) -> Optional[Callable[[MoveEvent], None]]:
        if not verbose:
            return None

        verbose_board = self.config.verbose_board

        def on_move(ev: MoveEvent):
            side_label = "W" if ev.side == "white" else "B"
            score_tag  = ""
            if ev.record.score_cp is not None:
                v = ev.record.score_cp
                score_tag = f" [{'+' if v >= 0 else ''}{v / 100:.2f}]"
            elif ev.record.score_mate is not None:
                score_tag = f" [M{ev.record.score_mate}]"

            if ev.side == "white":
                prefix = f"  {ev.move_num:>3}."
            else:
                prefix = "      "

            depth_tag = f" d{ev.record.depth}" if ev.record.depth else ""
            print(f"{prefix} {side_label}:{ev.record.notation:<8}{score_tag}{depth_tag}")

            if verbose_board:
                # Print FEN
                print(f"         FEN: {ev.board.fen()}")

                # Print coloured Unicode board
                for line in _render_board(ev.board).splitlines():
                    print(f"         {line}")

                # Print PV as SAN (apply onto a scratch board copy).
                # pv_uci[0] is the move just played (already on the board),
                # so start from index 1 for the continuation.
                pv_uci = (ev.record.pv or "").split()
                if len(pv_uci) > 1:
                    import copy
                    scratch = copy.deepcopy(ev.board)
                    pv_san: list[str] = []
                    for move in pv_uci[1:6]:
                        try:
                            m = scratch.push_uci(move)
                            pv_san.append(m["notation"])
                        except Exception:
                            break
                    if pv_san:
                        print(f"         PV:  {' '.join(pv_san)}")
                print()

        return on_move
