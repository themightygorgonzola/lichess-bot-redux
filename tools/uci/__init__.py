"""
uci — modular UCI engine client library.

Designed to be dual-purpose:
  • Self-play / local tournaments  (UCIPlayer vs UCIPlayer via MatchRunner)
  • External API integration       (subclass Player and plug into MatchRunner / GameSession)

Quick start:
    from uci import UCIEngine, UCIPlayer, MatchRunner, MatchConfig, TimeControl

    engine = UCIEngine("build/lichess-bot.exe", threads=4, hash_mb=128)
    p1 = UCIPlayer(engine, label="Bot-A")
    p2 = UCIPlayer(UCIEngine("build/lichess-bot.exe", threads=1), label="Bot-B")

    config = MatchConfig(games=10, tc=TimeControl(movetime_ms=500))
    result = MatchRunner(p1, p2, config).run()
    print(result.summary())
"""

from .engine import UCIEngine, SearchResult
from .player import Player, UCIPlayer, TimeControl
from .board  import MinimalBoard
from .game   import GameSession, GameResult, GameStatus, MoveEvent
from .match  import MatchRunner, MatchConfig, MatchResult
from .epd    import (
    EpdEntry, EpdResult, PositionResult, EpdTester,
    parse_epd_file, parse_epd_string, parse_epd_line,
    builtin_entries, download_epd, KNOWN_SUITES, WAC300_BUNDLED,
)

__all__ = [
    "UCIEngine", "SearchResult",
    "Player", "UCIPlayer", "TimeControl",
    "MinimalBoard",
    "GameSession", "GameResult", "GameStatus", "MoveEvent",
    "EpdEntry", "EpdResult", "PositionResult", "EpdTester",
    "parse_epd_file", "parse_epd_string", "parse_epd_line",
    "builtin_entries", "download_epd", "KNOWN_SUITES", "WAC300_BUNDLED",
    "MatchRunner", "MatchConfig", "MatchResult",
]
