"""
uci/engine.py — Low-level UCI engine subprocess wrapper.

Handles process lifecycle, raw UCI message I/O, and blocking search calls.
Contains no game logic, no threading for external consumers, no HTTP coupling.
Thread-safe: a single lock serialises all UCI commands. Multiple concurrent
searches on the same engine process are not allowed (raise RuntimeError).

Usage:
    engine = UCIEngine("build/lichess-bot.exe", threads=4, hash_mb=128)
    engine.new_game()
    engine.position("startpos", moves=["e2e4", "e7e5"])
    result = engine.go(movetime_ms=500)
    print(result.bestmove, result.score_cp, result.depth)
    engine.close()
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# SearchResult — returned by every go() call
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    bestmove:   Optional[str] = None
    ponder:     Optional[str] = None
    depth:      int           = 0
    seldepth:   int           = 0
    score_cp:   Optional[int] = None   # centipawns (None if mate score)
    score_mate: Optional[int] = None   # mate in N (None if cp score)
    nodes:      int           = 0
    nps:        int           = 0
    hashfull:   int           = 0
    time_ms:    int           = 0
    pv:         str           = ""

    @property
    def is_null_move(self) -> bool:
        """True when the engine has no legal move (checkmate / stalemate)."""
        return self.bestmove in (None, "0000", "(none)")

    @property
    def score_display(self) -> str:
        if self.score_mate is not None:
            return f"mate {self.score_mate}"
        if self.score_cp is not None:
            return f"cp {self.score_cp}"
        return "?"


# ---------------------------------------------------------------------------
# UCIEngine
# ---------------------------------------------------------------------------

class UCIEngine:
    """
    Manages a single UCI engine subprocess.

    All public methods are thread-safe via an internal lock. Only one go()
    call may be active at a time; attempting a second raises RuntimeError.
    """

    DEFAULT_EXTRA_PATHS = [r"C:\mingw64\bin"]

    def __init__(
        self,
        engine_path: str,
        threads: int   = 1,
        hash_mb: int   = 64,
        extra_options: Optional[dict] = None,
        use_nnue: bool = True,
    ):
        self.engine_path   = os.path.abspath(engine_path)
        self._threads      = threads
        self._hash_mb      = hash_mb
        self._extra_opts   = extra_options or {}
        self._use_nnue     = use_nnue
        self._lock         = threading.Lock()
        self._searching    = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

        # Track the engine's declared name/author from "uci" response
        self.name:   str = os.path.basename(engine_path)
        self.author: str = ""

        self._start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _start(self):
        env = os.environ.copy()
        for p in self.DEFAULT_EXTRA_PATHS:
            if os.path.isdir(p) and p not in env.get("PATH", ""):
                env["PATH"] = p + os.pathsep + env.get("PATH", "")

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            [self.engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
            creationflags=flags,
        )

        self._send("uci")
        for line in self._iter_until("uciok"):
            if line.startswith("id name"):
                self.name = line[len("id name"):].strip()
            elif line.startswith("id author"):
                self.author = line[len("id author"):].strip()

        self._apply_options()
        self._isready()

    def _apply_options(self):
        self._send(f"setoption name Threads value {self._threads}")
        self._send(f"setoption name Hash value {self._hash_mb}")
        if not self._use_nnue:
            self._send("setoption name UseNNUE value false")
        for name, value in self._extra_opts.items():
            self._send(f"setoption name {name} value {value}")

    def close(self):
        """Shut down the engine process gracefully."""
        try:
            if self._proc and self._proc.poll() is None:
                self._send("quit")
                self._proc.wait(timeout=5)
        except Exception:
            if self._proc:
                self._proc.kill()

    def restart(self):
        """Kill and restart the engine process."""
        self.close()
        self._start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # UCI commands
    # ------------------------------------------------------------------

    def set_option(self, name: str, value):
        """Send setoption. Takes effect on the next search."""
        with self._lock:
            self._send(f"setoption name {name} value {value}")

    def new_game(self):
        """Send ucinewgame + isready to reset engine state."""
        with self._lock:
            self._send("ucinewgame")
            self._isready_nolock()

    def position(self, fen: str = "startpos", moves: Optional[list[str]] = None):
        """
        Set up the position.
        fen may be "startpos" or a complete FEN string.
        moves is a list of UCI move strings applied on top.
        """
        with self._lock:
            if fen == "startpos":
                cmd = "position startpos"
            else:
                cmd = f"position fen {fen}"
            if moves:
                cmd += " moves " + " ".join(moves)
            self._send(cmd)

    def go(
        self,
        movetime_ms: Optional[int] = None,
        depth:       Optional[int] = None,
        wtime:       Optional[int] = None,
        btime:       Optional[int] = None,
        winc:        Optional[int] = None,
        binc:        Optional[int] = None,
        movestogo:   Optional[int] = None,
        nodes:       Optional[int] = None,
    ) -> SearchResult:
        """
        Start a search and block until bestmove is received.
        Returns a SearchResult. Thread-safe: raises if already searching.
        """
        if self._searching.is_set():
            raise RuntimeError("Engine is already searching")

        with self._lock:
            parts = ["go"]
            if movetime_ms is not None: parts += ["movetime", str(movetime_ms)]
            if depth       is not None: parts += ["depth",    str(depth)]
            if wtime       is not None: parts += ["wtime",    str(wtime)]
            if btime       is not None: parts += ["btime",    str(btime)]
            if winc        is not None: parts += ["winc",     str(winc)]
            if binc        is not None: parts += ["binc",     str(binc)]
            if movestogo   is not None: parts += ["movestogo",str(movestogo)]
            if nodes       is not None: parts += ["nodes",    str(nodes)]

            result = SearchResult()
            self._searching.set()
            try:
                self._send(" ".join(parts))
                self._read_until_bestmove(result)
            finally:
                self._searching.clear()

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, cmd: str):
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError(
                f"Engine process is not running (exit={self._proc.returncode if self._proc else 'N/A'})"
            )
        self._proc.stdin.write(cmd + "\n")
        self._proc.stdin.flush()

    def _readline(self) -> str:
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(
                f"Engine process died unexpectedly (exit={self._proc.poll()})"
            )
        return line.rstrip("\n").rstrip("\r")

    def _iter_until(self, token: str):
        """Yield lines until a line starts with token (inclusive)."""
        while True:
            line = self._readline()
            yield line
            if line.startswith(token):
                return

    def _isready(self):
        with self._lock:
            self._isready_nolock()

    def _isready_nolock(self):
        self._send("isready")
        for _ in self._iter_until("readyok"):
            pass

    def _parse_info(self, line: str, result: SearchResult):
        parts = line.split()
        # Skip pure info-string lines — they don't carry search data and their
        # free-form text can contain keywords like "depth" or "score" that would
        # confuse the parser below.
        if len(parts) >= 2 and parts[1] == "string":
            return
        i = 1  # skip "info"

        def _int(s):
            """Return int(s) or None if s is not a valid integer token."""
            try:
                return int(s)
            except (ValueError, TypeError):
                return None

        while i < len(parts):
            tok = parts[i]
            if tok == "depth" and i + 1 < len(parts):
                v = _int(parts[i + 1])
                if v is not None: result.depth = v
                i += 2
            elif tok == "seldepth" and i + 1 < len(parts):
                v = _int(parts[i + 1])
                if v is not None: result.seldepth = v
                i += 2
            elif tok == "score" and i + 2 < len(parts):
                if parts[i + 1] == "cp":
                    v = _int(parts[i + 2])
                    if v is not None:
                        result.score_cp   = v
                        result.score_mate = None
                    i += 3
                elif parts[i + 1] == "mate":
                    v = _int(parts[i + 2])
                    if v is not None:
                        result.score_mate = v
                        result.score_cp   = None
                    i += 3
                else:
                    i += 1
            elif tok == "nodes" and i + 1 < len(parts):
                v = _int(parts[i + 1])
                if v is not None: result.nodes = v
                i += 2
            elif tok == "nps" and i + 1 < len(parts):
                v = _int(parts[i + 1])
                if v is not None: result.nps = v
                i += 2
            elif tok == "hashfull" and i + 1 < len(parts):
                v = _int(parts[i + 1])
                if v is not None: result.hashfull = v
                i += 2
            elif tok == "time" and i + 1 < len(parts):
                v = _int(parts[i + 1])
                if v is not None: result.time_ms = v
                i += 2
            elif tok == "pv":
                result.pv = " ".join(parts[i + 1:]); break
            else:
                i += 1

    def _read_until_bestmove(self, result: SearchResult):
        while True:
            line = self._readline()
            if line.startswith("info "):
                self._parse_info(line, result)
            elif line.startswith("bestmove"):
                parts = line.split()
                result.bestmove = parts[1] if len(parts) > 1 else None
                if len(parts) > 3 and parts[2] == "ponder":
                    result.ponder = parts[3]
                return

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
