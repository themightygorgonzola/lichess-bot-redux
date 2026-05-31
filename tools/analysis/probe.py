#!/usr/bin/env python3
"""
probe.py â€” Deep positional probe: timed tiered analysis of a FEN by both engines.

For each time tier (100ms, 500ms, 1s, 3s, 10s) it captures EVERY info line
from both engines â€” every depth iteration, every multipv rank â€” giving a
complete timeline of how each engine's understanding evolves.

Output:
  - Live streaming table per tier as results arrive
  - Full JSON dump to results/probe_<hash>.json for later analysis

Usage
-----
  python tools/probe.py "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"
  python tools/probe.py startpos --moves e2e4 e7e5 g1f3 b8c6 f1c4
  python tools/probe.py "FEN" --tiers 100 500 1000 3000 10000
  python tools/probe.py "FEN" --multipv 5          # top-5 lines per tier
  python tools/probe.py "FEN" --tiers 500 2000 --no-color --json-only
  python tools/probe.py "FEN" --sf-only             # skip Redux, SF only
  python tools/probe.py "FEN" --bot-only            # skip SF
  python tools/probe.py "FEN" --threads-sf 4        # SF threads (Redux always 1)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

# â”€â”€ UTF-8 stdout on Windows â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if sys.platform == "win32":
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    try:
        import ctypes as _c
        _c.windll.kernel32.SetConsoleMode(_c.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

_ROOT  = pathlib.Path(__file__).resolve().parent.parent.parent
_TOOLS = _ROOT / "tools"
sys.path.insert(0, str(_TOOLS))
sys.path.insert(0, str(_ROOT))

from engine_config import find_engine as _find_bot_engine

# â”€â”€ ANSI helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_NO_COLOR = False

def _c(code: str, t: str) -> str:
    return t if _NO_COLOR else f"\033[{code}m{t}\033[0m"

def green(t):    return _c("92", t)
def red(t):      return _c("91", t)
def yellow(t):   return _c("93", t)
def cyan(t):     return _c("96", t)
def bold(t):     return _c("1",  t)
def dim(t):      return _c("2",  t)
def magenta(t):  return _c("95", t)
def blue(t):     return _c("94", t)
def white(t):    return _c("97", t)

# â”€â”€ Score formatting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fmt_score(cp: Optional[int], mate: Optional[int], side_to_move: bool = True) -> str:
    """side_to_move=True means score is from STM perspective (positive = good for STM)."""
    if mate is not None:
        m = mate if side_to_move else -mate
        if m > 0: return green(f"+M{m}")
        if m < 0: return red(f"-M{abs(m)}")
        return yellow("M0")
    if cp is not None:
        v = cp if side_to_move else -cp
        sign = "+" if v > 0 else ""
        if v >= 100:  return green(f"{sign}{v/100:.2f}")
        if v <= -100: return red(f"{sign}{v/100:.2f}")
        return yellow(f"{sign}{v/100:.2f}")
    return dim("?")

# â”€â”€ Data model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import re

# â”€â”€ Diagnostic data models (from Redux 'info string' lines) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class AspirationEvent:
    """One aspiration window fail event."""
    kind: str     # "fail-low" or "fail-high"
    depth: int    = 0
    iteration: int = 0
    window: int   = 0
    alpha: int    = 0
    beta: int     = 0
    score: int    = 0

@dataclass
class RootMoveScore:
    """Score for one root move from TT probe after a depth iteration."""
    move: str     = ""
    score: int    = 0
    flag: str     = "?"    # E=exact, B=beta, A=alpha, ?=miss
    tt_depth: int = 0

@dataclass
class SearchDiag:
    """Pruning/reduction statistics for one depth iteration."""
    depth: int         = 0
    tt_cuts: int       = 0
    null_cuts: int     = 0
    rfp: int           = 0
    razoring: int      = 0
    probcut: int       = 0
    futility: int      = 0
    lmp: int           = 0
    see: int           = 0
    hist: int          = 0
    lmr: int           = 0
    lmr_re: int        = 0

@dataclass
class StaticEvalInfo:
    """Root static eval breakdown emitted at depth 1."""
    nnue: int          = 0
    hce: int           = 0
    correction: int    = 0
    adjusted_nnue: int = 0
    nnue_loaded: bool  = False

@dataclass
class MoveOrderEntry:
    """One move + its ordering score from score_moves()."""
    move: str  = ""
    score: int = 0

@dataclass
class ChildEvalEntry:
    """Per-root-move child position evaluation (NNUE vs HCE vs material)."""
    move: str  = ""
    nnue: int  = 0   # NNUE score from parent STM perspective
    hce:  int  = 0   # HCE score from parent STM perspective
    mat:  int  = 0   # Material balance from parent STM perspective
    corr: int  = 0   # Pawn correction history adjustment
    diff: int  = 0   # nnue - hce (positive = NNUE more optimistic than HCE)

@dataclass
class DiagnosticData:
    """All diagnostic data collected from one tier run."""
    static_eval: Optional[StaticEvalInfo] = None
    aspiration_events: list[AspirationEvent] = field(default_factory=list)
    root_move_tables: dict[int, list[RootMoveScore]] = field(default_factory=dict)  # depth -> list
    move_orders: dict[int, list[MoveOrderEntry]] = field(default_factory=dict)       # depth -> list
    search_diags: dict[int, SearchDiag] = field(default_factory=dict)                # depth -> diag
    child_evals: list[ChildEvalEntry]    = field(default_factory=list)


def _parse_diagnostic(line: str) -> Optional[tuple[str, object]]:
    """Parse an 'info string ...' diagnostic line from Redux.
    Returns (type_tag, parsed_object) or None."""
    if not line.startswith("info string "):
        return None
    payload = line[len("info string "):]

    # Static eval
    if payload.startswith("static_eval"):
        m = re.search(r"nnue (-?\d+)", payload)
        ev = StaticEvalInfo()
        if m: ev.nnue = int(m.group(1))
        m = re.search(r"hce (-?\d+)", payload)
        if m: ev.hce = int(m.group(1))
        m = re.search(r"correction (-?\d+)", payload)
        if m: ev.correction = int(m.group(1))
        m = re.search(r"adjusted_nnue (-?\d+)", payload)
        if m: ev.adjusted_nnue = int(m.group(1))
        m = re.search(r"nnue_loaded (\d)", payload)
        if m: ev.nnue_loaded = m.group(1) == "1"
        return ("static_eval", ev)

    # Aspiration events
    if payload.startswith("aspiration "):
        ev = AspirationEvent(kind="")
        if "fail-low" in payload:    ev.kind = "fail-low"
        elif "fail-high" in payload: ev.kind = "fail-high"
        for key in ("depth", "iter", "window", "alpha", "beta", "score"):
            m = re.search(rf"{key} (-?\d+)", payload)
            if m:
                val = int(m.group(1))
                if key == "iter":     ev.iteration = val
                elif key == "depth":  ev.depth = val
                elif key == "window": ev.window = val
                elif key == "alpha":  ev.alpha = val
                elif key == "beta":   ev.beta = val
                elif key == "score":  ev.score = val
        return ("aspiration", ev)

    # Root move score table
    if payload.startswith("rootmoves depth"):
        m = re.match(r"rootmoves depth (\d+)(.*)", payload)
        if not m: return None
        depth = int(m.group(1))
        rest = m.group(2).strip()
        entries = []
        for token in rest.split():
            # Format: move:score/FlagDepth  e.g. d7d6:-8/E9
            parts = token.split(":")
            if len(parts) != 2: continue
            move = parts[0]
            score_flag = parts[1]  # e.g. -8/E9 or -99999/?0
            sf = score_flag.split("/")
            score = int(sf[0]) if len(sf) >= 1 else 0
            flag = "?"
            tt_d = 0
            if len(sf) >= 2:
                flag_str = sf[1]
                flag = flag_str[0] if flag_str else "?"
                try: tt_d = int(flag_str[1:]) if len(flag_str) > 1 else 0
                except ValueError: tt_d = 0
            entries.append(RootMoveScore(move=move, score=score, flag=flag, tt_depth=tt_d))
        return ("rootmoves", (depth, entries))

    # Move ordering
    if payload.startswith("moveorder depth"):
        m = re.match(r"moveorder depth (\d+)(.*)", payload)
        if not m: return None
        depth = int(m.group(1))
        rest = m.group(2).strip()
        entries = []
        for token in rest.split():
            parts = token.split(":")
            if len(parts) != 2: continue
            entries.append(MoveOrderEntry(move=parts[0], score=int(parts[1])))
        return ("moveorder", (depth, entries))

    # Search diagnostics
    if payload.startswith("searchdiag depth"):
        sd = SearchDiag()
        for key in ("depth", "tt_cuts", "null_cuts", "rfp", "razoring", "probcut",
                     "futility", "lmp", "see", "hist", "lmr", "lmr_re"):
            m = re.search(rf"{key} (-?\d+)", payload)
            if m: setattr(sd, key, int(m.group(1)))
        return ("searchdiag", sd)

    # Child position evaluations
    if payload.startswith("childeval"):
        ce = ChildEvalEntry()
        m = re.search(r"move (\S+)", payload)
        if m: ce.move = m.group(1)
        for key in ("nnue", "hce", "mat", "corr", "diff"):
            m = re.search(rf"{key} (-?\d+)", payload)
            if m: setattr(ce, key, int(m.group(1)))
        return ("childeval", ce)

    return None


# â”€â”€ Data model â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class InfoSnapshot:
    """One 'info ...' line captured from the engine output stream."""
    wall_ms:    float          # ms since go command was sent
    depth:      int   = 0
    seldepth:   int   = 0
    multipv:    int   = 1      # rank (1 = best)
    score_cp:   Optional[int]  = None
    score_mate: Optional[int]  = None
    score_bound: str  = ""     # "lowerbound" | "upperbound" | ""
    nodes:      int   = 0
    nps:        int   = 0
    hashfull:   int   = 0      # permill (0-1000)
    tbhits:     int   = 0
    time_ms:    int   = 0
    pv:         list[str] = field(default_factory=list)

    def score_display(self) -> str:
        return fmt_score(self.score_cp, self.score_mate)

    def pv_str(self, n: int = 5) -> str:
        return " ".join(self.pv[:n]) if self.pv else ""


@dataclass
class TierResult:
    """All snapshots captured during one movetime tier."""
    engine_name: str
    fen:         str
    movetime_ms: int
    bestmove:    Optional[str]          = None
    ponder:      Optional[str]          = None
    snapshots:   list[InfoSnapshot]     = field(default_factory=list)
    elapsed_ms:  float                  = 0.0
    diagnostics: DiagnosticData         = field(default_factory=DiagnosticData)

    @property
    def final_depth(self) -> int:
        if not self.snapshots: return 0
        return max(s.depth for s in self.snapshots)

    @property
    def final_seldepth(self) -> int:
        if not self.snapshots: return 0
        return max(s.seldepth for s in self.snapshots if s.multipv == 1)

    @property
    def final_nps(self) -> int:
        rank1 = [s for s in self.snapshots if s.multipv == 1]
        return rank1[-1].nps if rank1 else 0

    @property
    def final_nodes(self) -> int:
        rank1 = [s for s in self.snapshots if s.multipv == 1]
        return rank1[-1].nodes if rank1 else 0

    @property
    def final_score_cp(self) -> Optional[int]:
        rank1 = [s for s in self.snapshots if s.multipv == 1]
        return rank1[-1].score_cp if rank1 else None

    @property
    def final_score_mate(self) -> Optional[int]:
        rank1 = [s for s in self.snapshots if s.multipv == 1]
        return rank1[-1].score_mate if rank1 else None

    def depth_timeline(self) -> list[InfoSnapshot]:
        """One entry per depth per multipv rank â€” last snapshot seen at each depth."""
        seen: dict[tuple[int, int], InfoSnapshot] = {}
        for s in self.snapshots:
            seen[(s.depth, s.multipv)] = s
        return sorted(seen.values(), key=lambda s: (s.depth, s.multipv))

    def top_pvs(self, n: int = 10) -> list[InfoSnapshot]:
        """Latest snapshot for each multipv rank, best n."""
        seen: dict[int, InfoSnapshot] = {}
        for s in self.snapshots:
            seen[s.multipv] = s
        return sorted(seen.values(), key=lambda s: s.multipv)[:n]


# â”€â”€ Raw UCI engine with full info capture â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _parse_info(line: str, t0: float) -> Optional[InfoSnapshot]:
    parts = line.split()
    if len(parts) < 2 or parts[0] != "info":
        return None
    snap = InfoSnapshot(wall_ms=round((time.perf_counter() - t0) * 1000, 1))
    i = 1
    while i < len(parts):
        tok = parts[i]
        if   tok == "depth"      and i+1 < len(parts): snap.depth      = int(parts[i+1]);  i += 2
        elif tok == "seldepth"   and i+1 < len(parts): snap.seldepth   = int(parts[i+1]);  i += 2
        elif tok == "multipv"    and i+1 < len(parts): snap.multipv    = int(parts[i+1]);  i += 2
        elif tok == "nodes"      and i+1 < len(parts): snap.nodes      = int(parts[i+1]);  i += 2
        elif tok == "nps"        and i+1 < len(parts): snap.nps        = int(parts[i+1]);  i += 2
        elif tok == "hashfull"   and i+1 < len(parts): snap.hashfull   = int(parts[i+1]);  i += 2
        elif tok == "tbhits"     and i+1 < len(parts): snap.tbhits     = int(parts[i+1]);  i += 2
        elif tok == "time"       and i+1 < len(parts): snap.time_ms    = int(parts[i+1]);  i += 2
        elif tok == "score"      and i+2 < len(parts):
            stype = parts[i+1]
            if stype in ("cp", "mate"):
                val = int(parts[i+2])
                if stype == "cp":   snap.score_cp   = val
                else:               snap.score_mate  = val
                i += 3
                if i < len(parts) and parts[i] in ("lowerbound", "upperbound"):
                    snap.score_bound = parts[i]; i += 1
            else:
                i += 1
        elif tok == "pv":
            snap.pv = parts[i+1:]; break
        else:
            i += 1
    return snap if snap.depth > 0 else None


class RawEngine:
    """Minimal subprocess engine that captures every info line."""

    _EXTRA = [r"C:\mingw64\bin"]

    def __init__(self, path: str, threads: int = 1, hash_mb: int = 64,
                 extra_opts: Optional[dict] = None, multipv: int = 1):
        self._path = os.path.abspath(path)
        self._threads   = threads
        self._hash_mb   = hash_mb
        self._extra_opts = extra_opts or {}
        self._multipv   = multipv
        self.name = os.path.basename(path)
        self.author = ""
        self._proc: Optional[subprocess.Popen] = None
        self._start()

    def _start(self):
        env = os.environ.copy()
        for p in self._EXTRA:
            if os.path.isdir(p) and p not in env.get("PATH", ""):
                env["PATH"] = p + os.pathsep + env.get("PATH", "")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            [self._path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env=env, creationflags=flags,
        )
        self._send("uci")
        while True:
            ln = self._readline()
            if ln.startswith("id name"):   self.name   = ln[8:].strip()
            elif ln.startswith("id author"): self.author = ln[10:].strip()
            elif ln.startswith("uciok"):   break
        self._send(f"setoption name Threads value {self._threads}")
        self._send(f"setoption name Hash value {self._hash_mb}")
        self._send(f"setoption name MultiPV value {self._multipv}")
        for k, v in self._extra_opts.items():
            self._send(f"setoption name {k} value {v}")
        self._send("isready")
        while True:
            ln = self._readline()
            if ln.startswith("readyok"): break

    def _send(self, cmd: str):
        self._proc.stdin.write(cmd + "\n")
        self._proc.stdin.flush()

    def _readline(self) -> str:
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Engine {self.name!r} died unexpectedly")
        return line.rstrip("\n\r")

    def probe(self, fen: str, moves: list[str], movetime_ms: int) -> TierResult:
        """Run one movetime search and capture all info lines."""
        # Reset
        self._send("ucinewgame")
        self._send("isready")
        while True:
            if self._readline().startswith("readyok"): break

        # Set position
        pos = f"position fen {fen}" if fen != "startpos" else "position startpos"
        if moves:
            pos += " moves " + " ".join(moves)
        self._send(pos)

        result = TierResult(engine_name=self.name, fen=fen, movetime_ms=movetime_ms)
        t0 = time.perf_counter()
        self._send(f"go movetime {movetime_ms}")

        while True:
            line = self._readline()
            if line.startswith("info string"):
                diag = _parse_diagnostic(line)
                if diag:
                    tag, obj = diag
                    if tag == "static_eval":
                        result.diagnostics.static_eval = obj
                    elif tag == "aspiration":
                        result.diagnostics.aspiration_events.append(obj)
                    elif tag == "rootmoves":
                        depth, entries = obj
                        result.diagnostics.root_move_tables[depth] = entries
                    elif tag == "moveorder":
                        depth, entries = obj
                        result.diagnostics.move_orders[depth] = entries
                    elif tag == "searchdiag":
                        result.diagnostics.search_diags[obj.depth] = obj
                    elif tag == "childeval":
                        result.diagnostics.child_evals.append(obj)
            elif line.startswith("info "):
                snap = _parse_info(line, t0)
                if snap:
                    result.snapshots.append(snap)
            elif line.startswith("bestmove"):
                parts = line.split()
                result.bestmove = parts[1] if len(parts) > 1 else None
                if len(parts) > 3 and parts[2] == "ponder":
                    result.ponder = parts[3]
                break

        result.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result

    def close(self):
        try:
            if self._proc and self._proc.poll() is None:
                self._send("quit")
                self._proc.wait(timeout=3)
        except Exception:
            if self._proc: self._proc.kill()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


# â”€â”€ Engine discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_SF_CANDIDATES = [
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64-avx2.exe",
    "engines/stockfish-17.1/stockfish/stockfish-windows-x86-64.exe",
    "engines/stockfish/stockfish.exe",
]

def find_sf() -> str:
    for rel in _SF_CANDIDATES:
        p = _ROOT / rel
        if p.exists(): return str(p)
    raise FileNotFoundError("Stockfish not found. Expected: " + _SF_CANDIDATES[0])

def find_bot() -> str:
    return _find_bot_engine()


# â”€â”€ Board rendering â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def render_board(fen: str, last_move: Optional[str] = None) -> str:
    try:
        import chess
        board = chess.Board(fen) if fen != "startpos" else chess.Board()
        lm = chess.Move.from_uci(last_move) if last_move else None
    except Exception:
        return f"FEN: {fen}"

    if _NO_COLOR:
        return str(board)

    LIGHT = "\033[48;5;223m"; DARK = "\033[48;5;130m"; HL = "\033[48;5;184m"
    RST = "\033[0m"; WP = "\033[97m"; BP = "\033[30m"
    UNI = {"P":"â™™","N":"â™˜","B":"â™—","R":"â™–","Q":"â™•","K":"â™”",
           "p":"â™Ÿ","n":"â™ž","b":"â™","r":"â™œ","q":"â™›","k":"â™š"}
    hl = set()
    if lm:
        hl.add(lm.from_square); hl.add(lm.to_square)

    lines = []
    for r in range(7, -1, -1):
        row = f" {r+1} "
        for f in range(8):
            sq = chess.square(f, r)
            bg = HL if sq in hl else (LIGHT if (r+f) % 2 == 1 else DARK)
            pc = board.piece_at(sq)
            if pc:
                sym = UNI.get(pc.symbol(), pc.symbol())
                fg = WP if pc.color == chess.WHITE else BP
                row += f"{bg}{fg} {sym} {RST}"
            else:
                row += f"{bg}   {RST}"
        lines.append(row)
    lines.append("   " + "  ".join(" abcdefgh"[f+1] for f in range(8)))
    return "\n".join(lines)


# â”€â”€ Display helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _sep(w: int = 80, ch: str = "-") -> str:
    return dim(ch * w)

def _tier_header(ms: int) -> str:
    label = f" {ms}ms "
    pad   = "=" * ((78 - len(label)) // 2)
    return bold(cyan(f"{pad}{label}{pad}"))

def _fmt_nodes(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)

def _fmt_nps(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)

def _fmt_hash(h: int) -> str:
    return f"{h/10:.1f}%" if h else "0%"

def _pv_with_board(pv: list[str], fen: str, n: int = 8) -> str:
    """Render first n moves of pv as SAN notation if chess lib available."""
    try:
        import chess
        board = chess.Board(fen) if fen != "startpos" else chess.Board()
        sans = []
        for uci in pv[:n]:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves: break
            sans.append(board.san(move))
            board.push(move)
        return " ".join(sans)
    except Exception:
        return " ".join(pv[:n])


def print_tier_results(tier_ms: int, results: dict[str, TierResult], multipv: int, fen: str):
    """Print rich side-by-side breakdown for one time tier."""
    names  = list(results.keys())
    N      = len(names)
    w      = 78 if N == 1 else 78

    print()
    print(_tier_header(tier_ms))

    # â”€â”€ Summary row â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    for eng, tr in results.items():
        score_str = fmt_score(tr.final_score_cp, tr.final_score_mate)
        bm_str    = bold(white(tr.bestmove or "none"))
        pm_str    = dim(f"ponder={tr.ponder}") if tr.ponder else ""
        nps_str   = _fmt_nps(tr.final_nps)
        nodes_str = _fmt_nodes(tr.final_nodes)
        print(
            f"  {bold(eng):30s}"
            f"  bestmove={bm_str}  {pm_str}"
        )
        print(
            f"  {'':30s}"
            f"  score={score_str}  "
            f"depth={bold(str(tr.final_depth))}/{tr.final_seldepth}  "
            f"nodes={nodes_str}  "
            f"nps={nps_str}  "
            f"elapsed={tr.elapsed_ms:.0f}ms"
        )

    # â”€â”€ Agreement / disagreement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if N == 2:
        vals = list(results.values())
        if vals[0].bestmove and vals[1].bestmove:
            if vals[0].bestmove == vals[1].bestmove:
                print(f"  {green('AGREE')}  both chose {bold(vals[0].bestmove)}")
            else:
                print(
                    f"  {red('DIFFER')}  "
                    f"{names[0]}={bold(vals[0].bestmove or '?')}  "
                    f"{names[1]}={bold(vals[1].bestmove or '?')}"
                )

    # â”€â”€ Top PV lines â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print()
    print(f"  {'Rank':<5} {'Score':>8}  {'Depth':>6}  {'SelD':>5}  {'PV'}")
    print("  " + _sep(72))

    for eng, tr in results.items():
        print(f"  {bold(cyan(eng))}")
        pvs = tr.top_pvs(multipv)
        for snap in pvs:
            score_s  = fmt_score(snap.score_cp, snap.score_mate)
            bound_s  = dim(f"[{snap.score_bound[0].upper()}]") if snap.score_bound else "   "
            pv_san   = _pv_with_board(snap.pv, fen)
            pv_uci   = dim(" ".join(snap.pv[:6]))
            print(
                f"  #{snap.multipv:<4d} {score_s:>8}  {bound_s} "
                f"d={snap.depth:>2}/{snap.seldepth:<2}  "
                f"nodes={_fmt_nodes(snap.nodes):<7}  "
                f"nps={_fmt_nps(snap.nps):<8}  "
                f"hash={_fmt_hash(snap.hashfull):<6}  "
                f"{pv_san}"
            )
            print(f"  {'':5}  {dim('uci:')} {pv_uci}")

    # â”€â”€ Depth timeline (rank 1 only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print()
    print(f"  {bold('Depth timeline (rank 1)')}  â€” score evolution per depth")
    print(f"  {'Depth':>6}  {'Score':>9}  {'Nodes':>9}  {'NPS':>9}  {'Hash':>6}  {'Time':>7}  PV (first 4)")
    print("  " + _sep(72))

    for eng, tr in results.items():
        print(f"  {bold(cyan(eng))}")
        timeline = [s for s in tr.depth_timeline() if s.multipv == 1]
        for snap in timeline:
            score_s = fmt_score(snap.score_cp, snap.score_mate)
            bound_s = dim(f"[{snap.score_bound[0].upper()}]") if snap.score_bound else "   "
            pv4     = _pv_with_board(snap.pv, fen, 4)
            print(
                f"  {snap.depth:>6}  {score_s:>9}  {bound_s}"
                f"  {_fmt_nodes(snap.nodes):>9}"
                f"  {_fmt_nps(snap.nps):>9}"
                f"  {_fmt_hash(snap.hashfull):>6}"
                f"  {snap.time_ms:>6}ms"
                f"  {pv4}"
            )

    # â”€â”€ Engine diagnostics (Redux only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    for eng, tr in results.items():
        diag = tr.diagnostics
        has_diag = (diag.static_eval is not None
                    or diag.aspiration_events
                    or diag.search_diags
                    or diag.child_evals)
        if not has_diag:
            continue

        print()
        print(f"  {bold(magenta(f'{eng} Diagnostics'))}")
        print("  " + _sep(72))

        # Static eval
        if diag.static_eval:
            se = diag.static_eval
            nnue_s = green(f"{se.nnue:+d}") if se.nnue > 0 else red(f"{se.nnue:+d}") if se.nnue < 0 else yellow("0")
            hce_s  = green(f"{se.hce:+d}")  if se.hce > 0  else red(f"{se.hce:+d}")  if se.hce < 0  else yellow("0")
            print(f"  {bold('Static eval:')}  NNUE={nnue_s}  HCE={hce_s}  "
                  f"corr={se.correction:+d}  adj_nnue={se.adjusted_nnue:+d}  "
                  f"loaded={'yes' if se.nnue_loaded else 'NO'}")

        # Child position evaluations
        if diag.child_evals:
            sorted_ce = sorted(diag.child_evals, key=lambda c: c.diff, reverse=True)
            print(f"\n  {bold('Child position evals (parent STM perspective):')  }")
            print(f"  {'Move':<8}  {'NNUE':>7}  {'HCE':>7}  {'Material':>9}  {'Corr':>6}  {'NNUE-HCE':>9}  Bias")
            print("  " + _sep(64))
            for ce in sorted_ce:
                nnue_s = green(f"{ce.nnue:+d}") if ce.nnue > 0 else red(f"{ce.nnue:+d}") if ce.nnue < 0 else yellow("0")
                hce_s  = green(f"{ce.hce:+d}")  if ce.hce > 0  else red(f"{ce.hce:+d}")  if ce.hce < 0  else yellow("0")
                diff_s = red(f"{ce.diff:+d}") if abs(ce.diff) > 50 else yellow(f"{ce.diff:+d}") if abs(ce.diff) > 20 else green(f"{ce.diff:+d}")
                bias_s = red("HIGH") if abs(ce.diff) > 80 else yellow("MED") if abs(ce.diff) > 30 else green("low")
                print(f"  {ce.move:<8}  {nnue_s:>7}  {hce_s:>7}  {ce.mat:>+9}  {ce.corr:>+6}  {diff_s:>9}  {bias_s}")

        # Aspiration window events
        if diag.aspiration_events:
            print(f"\n  {bold('Aspiration window events:')}")
            for ev in diag.aspiration_events:
                kind_s = red("FAIL-LOW") if ev.kind == "fail-low" else yellow("FAIL-HIGH")
                print(f"    d={ev.depth} iter={ev.iteration}  {kind_s}  "
                      f"window={ev.window}  [{ev.alpha}, {ev.beta}]  score={ev.score}")

        # Search diagnostics table
        if diag.search_diags:
            print(f"\n  {bold('Pruning & reduction statistics by depth:')}")
            print(f"  {'Depth':>5}  {'TT':>7}  {'Null':>6}  {'RFP':>7}  "
                  f"{'Razor':>6}  {'ProbC':>6}  {'Futl':>7}  "
                  f"{'LMP':>7}  {'SEE':>6}  {'Hist':>6}  "
                  f"{'LMR':>7}  {'LMR-re':>7}")
            print("  " + _sep(92))
            for d in sorted(diag.search_diags.keys()):
                sd = diag.search_diags[d]
                print(f"  {sd.depth:>5}  {sd.tt_cuts:>7,}  {sd.null_cuts:>6,}  "
                      f"{sd.rfp:>7,}  {sd.razoring:>6,}  {sd.probcut:>6,}  "
                      f"{sd.futility:>7,}  {sd.lmp:>7,}  {sd.see:>6,}  "
                      f"{sd.hist:>6,}  {sd.lmr:>7,}  {sd.lmr_re:>7,}")

        # Root move score table (show for the deepest completed depth)
        if diag.root_move_tables:
            max_d = max(diag.root_move_tables.keys())
            rms = diag.root_move_tables[max_d]
            # Sort by score descending
            rms_sorted = sorted(rms, key=lambda r: r.score, reverse=True)
            print(f"\n  {bold(f'Root move scores at depth {max_d}:')}  (from TT)")
            print(f"  {'#':>3}  {'Move':<8}  {'Score':>7}  {'Flag':<5}  {'TTd':>4}")
            print("  " + _sep(40))
            for i, rm in enumerate(rms_sorted[:20], 1):
                sc = rm.score
                if sc <= -99000:
                    score_s = dim("n/a")
                else:
                    score_s = green(f"{sc:+d}") if sc > 0 else red(f"{sc:+d}") if sc < 0 else yellow("0")
                flag_map = {"E": "Exact", "B": "Beta", "A": "Alpha", "?": "Miss"}
                flag_s = flag_map.get(rm.flag, rm.flag)
                print(f"  {i:>3}  {rm.move:<8}  {score_s:>7}  {flag_s:<5}  {rm.tt_depth:>4}")
            if len(rms_sorted) > 20:
                print(f"  ... and {len(rms_sorted)-20} more moves")

        # Move ordering at deepest depth
        if diag.move_orders:
            max_d = max(diag.move_orders.keys())
            mos = diag.move_orders[max_d]
            print(f"\n  {bold(f'Move ordering at depth {max_d}:')}  (pre-search heuristic)")
            print(f"  {'#':>3}  {'Move':<8}  {'Score':>12}  {'Category'}")
            print("  " + _sep(50))
            for i, mo in enumerate(mos[:15], 1):
                s = mo.score
                if s >= 10_000_000:     cat = bold("TT move")
                elif s >= 1_000_000:    cat = green("good capture")
                elif s >= 950_000:      cat = cyan("capture+check")
                elif s >= 900_000:      cat = blue("killer 1")
                elif s >= 800_000:      cat = blue("killer 2")
                elif s >= 700_000:      cat = magenta("countermove")
                elif s <= -1_000_000:   cat = red("bad capture")
                else:                   cat = dim("quiet/history")
                print(f"  {i:>3}  {mo.move:<8}  {s:>12,}  {cat}")
            if len(mos) > 15:
                print(f"  ... and {len(mos)-15} more moves")


# â”€â”€ JSON serialization â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _tier_to_dict(tr: TierResult) -> dict:
    d = {
        "engine":       tr.engine_name,
        "fen":          tr.fen,
        "movetime_ms":  tr.movetime_ms,
        "bestmove":     tr.bestmove,
        "ponder":       tr.ponder,
        "elapsed_ms":   tr.elapsed_ms,
        "final_depth":  tr.final_depth,
        "final_seldepth": tr.final_seldepth,
        "final_nps":    tr.final_nps,
        "final_nodes":  tr.final_nodes,
        "final_score_cp":   tr.final_score_cp,
        "final_score_mate": tr.final_score_mate,
        "snapshots": [asdict(s) for s in tr.snapshots],
    }
    # Include diagnostics if present
    diag = tr.diagnostics
    diag_dict = {}
    if diag.static_eval:
        diag_dict["static_eval"] = asdict(diag.static_eval)
    if diag.aspiration_events:
        diag_dict["aspiration_events"] = [asdict(e) for e in diag.aspiration_events]
    if diag.root_move_tables:
        diag_dict["root_move_tables"] = {
            str(depth): [asdict(rm) for rm in rms]
            for depth, rms in diag.root_move_tables.items()
        }
    if diag.move_orders:
        diag_dict["move_orders"] = {
            str(depth): [asdict(mo) for mo in mos]
            for depth, mos in diag.move_orders.items()
        }
    if diag.search_diags:
        diag_dict["search_diags"] = {
            str(depth): asdict(sd)
            for depth, sd in diag.search_diags.items()
        }
    if diag.child_evals:
        diag_dict["child_evals"] = [asdict(ce) for ce in diag.child_evals]
    if diag_dict:
        d["diagnostics"] = diag_dict
    return d


# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DEFAULT_TIERS = [100, 500, 1000, 3000, 10000]

def main():
    ap = argparse.ArgumentParser(description="Deep positional probe â€” tiered analysis by both engines")
    ap.add_argument("fen",             nargs="?", default="startpos",
                    help="FEN string or 'startpos'")
    ap.add_argument("--moves",         nargs="*", default=[],
                    help="UCI moves to play from the FEN before probing")
    ap.add_argument("--tiers",         nargs="+", type=int, default=DEFAULT_TIERS,
                    help="Movetime tiers in ms (default: 100 500 1000 3000 10000)")
    ap.add_argument("--multipv",       type=int, default=5,
                    help="Number of PV lines per engine (default: 5)")
    ap.add_argument("--sf-only",       action="store_true")
    ap.add_argument("--bot-only",      action="store_true")
    ap.add_argument("--threads-sf",    type=int, default=1,
                    help="Stockfish threads (default: 1 for fair comparison)")
    ap.add_argument("--hash",          type=int, default=128,
                    help="Hash table MB for both engines (default: 128)")
    ap.add_argument("--no-color",      action="store_true")
    ap.add_argument("--json-only",     action="store_true",
                    help="Suppress terminal output, only write JSON")
    ap.add_argument("--out",           default=None,
                    help="Override output JSON path")
    ap.add_argument("--no-json",       action="store_true",
                    help="Don't write JSON output file")
    ap.add_argument("--sf-engine",     default=None, help="Stockfish binary path override")
    ap.add_argument("--bot-engine",    default=None, help="Redux binary path override")
    args = ap.parse_args()

    global _NO_COLOR
    if args.no_color: _NO_COLOR = True

    fen   = args.fen
    moves = args.moves or []

    # â”€â”€ Discover engines â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    engines: dict[str, str] = {}
    if not args.sf_only:
        try:
            engines["Redux"] = args.bot_engine or find_bot()
        except FileNotFoundError as e:
            print(f"[warn] Bot engine not found: {e}  (use --bot-engine or make.ps1 build)")
    if not args.bot_only:
        try:
            engines["Stockfish"] = args.sf_engine or find_sf()
        except FileNotFoundError as e:
            print(f"[warn] Stockfish not found: {e}")

    if not engines:
        print("[error] No engines available. Aborting.")
        sys.exit(1)

    # â”€â”€ Header â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not args.json_only:
        print()
        print(bold("=" * 78))
        print(bold(f"  PROBE  â€”  {fen[:72]}"))
        if moves:
            print(bold(f"  moves:   {' '.join(moves)}"))
        print(bold(f"  tiers:   {' '.join(str(t)+'ms' for t in args.tiers)}"))
        print(bold(f"  multipv: {args.multipv}   hash: {args.hash}MB"))
        print(bold(f"  engines: {', '.join(engines.keys())}"))
        print(bold("=" * 78))
        print()
        # Compute effective FEN after moves for board rendering
        try:
            import chess
            b = chess.Board(fen) if fen != "startpos" else chess.Board()
            for mv in moves:
                b.push(chess.Move.from_uci(mv))
            display_fen = b.fen()
            print(render_board(display_fen))
            side = "White" if b.turn == chess.WHITE else "Black"
            print(f"\n  {bold('Side to move:')} {side}   "
                  f"{bold('FEN:')} {dim(display_fen)}")
        except Exception:
            display_fen = fen
        print()

    # â”€â”€ Start engines â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    live_engines: dict[str, RawEngine] = {}
    for name, path in engines.items():
        t = args.threads_sf if name == "Stockfish" else 1
        extra = {}
        if name == "Redux":
            nnue = find_bot().replace("redux.exe", "nn.bin").replace("lichess-bot.exe", "nn.bin")
            if os.path.isfile(nnue):
                extra["EvalFile"] = nnue
        live_engines[name] = RawEngine(path, threads=t, hash_mb=args.hash,
                                       extra_opts=extra, multipv=args.multipv)
        if not args.json_only:
            print(f"  {green('[ready]')} {bold(name)} ({live_engines[name].name})  {dim(path)}")

    if not args.json_only:
        print()

    # â”€â”€ Run tiers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    all_results: list[dict] = []   # for JSON

    try:
        for tier_ms in args.tiers:
            tier_data: dict[str, TierResult] = {}

            for eng_name, eng in live_engines.items():
                if not args.json_only:
                    # Print live progress indicator
                    print(f"  searching {bold(eng_name)}  @{tier_ms}ms...", end="\r", flush=True)
                tr = eng.probe(fen, moves, tier_ms)
                tier_data[eng_name] = tr

            if not args.json_only:
                print(" " * 60, end="\r")  # clear progress line
                # Compute display_fen for PV rendering
                try:
                    import chess
                    b = chess.Board(fen) if fen != "startpos" else chess.Board()
                    for mv in moves: b.push(chess.Move.from_uci(mv))
                    display_fen = b.fen()
                except Exception:
                    display_fen = fen
                print_tier_results(tier_ms, tier_data, args.multipv, display_fen)

            all_results.append({
                "tier_ms": tier_ms,
                "engines": {n: _tier_to_dict(tr) for n, tr in tier_data.items()},
            })

    finally:
        for eng in live_engines.values():
            eng.close()

    # â”€â”€ JSON output â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not args.no_json:
        results_dir = _ROOT / "results"
        results_dir.mkdir(exist_ok=True)

        fen_hash = hashlib.sha1(f"{fen}{''.join(moves)}".encode()).hexdigest()[:8]
        out_path = pathlib.Path(args.out) if args.out else results_dir / f"probe_{fen_hash}.json"

        payload = {
            "fen":      fen,
            "moves":    moves,
            "tiers_ms": args.tiers,
            "multipv":  args.multipv,
            "hash_mb":  args.hash,
            "tiers":    all_results,
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        if not args.json_only:
            print()
            print(f"  {green('[saved]')} {out_path}")

    if not args.json_only:
        print()


if __name__ == "__main__":
    main()
