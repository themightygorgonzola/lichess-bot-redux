#!/usr/bin/env python3
"""
test_syzygy_sprt.py — Verification tests for:
  1. Syzygy UCI integration (no TB files required — tests graceful no-op)
  2. SPRTState correctness (statistical properties)
  3. sprt_tune.py --help smoke test
  4. Syzygy probe with a real known KPK position (skipped if no TB path given)

Run:
    python tools/tests/test_syzygy_sprt.py
    python tools/tests/test_syzygy_sprt.py --tb-path /path/to/syzygy_tables

Exit code 0 = all tests passed (or skipped where TB files absent).
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time
import threading

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
HCE_EXE = os.path.join(REPO, "bot", "engine", "redux-hce.exe")
TOOLS_DIR = os.path.join(REPO, "tools")

if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from sprt import SPRTState  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

_results: list[tuple[str, str, str]] = []  # (name, status, note)


def result(name: str, ok: bool, note: str = "") -> None:
    tag = PASS if ok else FAIL
    _results.append((name, "PASS" if ok else "FAIL", note))
    print(f"  [{tag}] {name}" + (f"  ({note})" if note else ""))


def skip(name: str, note: str = "") -> None:
    _results.append((name, "SKIP", note))
    print(f"  [{SKIP}] {name}" + (f"  ({note})" if note else ""))


# ---------------------------------------------------------------------------
# UCI interaction helper
# ---------------------------------------------------------------------------

class UCISession:
    """Minimal UCI subprocess wrapper for testing."""

    def __init__(self, exe: str, timeout: float = 5.0):
        self._p = subprocess.Popen(
            [exe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._timeout = timeout
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()

    def _reader(self) -> None:
        for line in iter(self._p.stdout.readline, ""):
            with self._lock:
                self._lines.append(line.rstrip("\r\n"))

    def send(self, cmd: str, delay: float = 0.05) -> None:
        self._p.stdin.write(cmd + "\n")
        self._p.stdin.flush()
        time.sleep(delay)

    def collect(self, until: str, timeout: float | None = None) -> list[str]:
        """Collect output lines until a line containing `until` is seen."""
        deadline = time.time() + (timeout or self._timeout)
        seen: list[str] = []
        while time.time() < deadline:
            with self._lock:
                for line in self._lines:
                    if line not in seen:
                        seen.append(line)
                    if until in line:
                        return seen
            time.sleep(0.02)
        return seen

    def quit(self) -> None:
        try:
            self._p.stdin.write("quit\n")
            self._p.stdin.flush()
        except OSError:
            pass
        try:
            self._p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._p.kill()


# ===========================================================================
# Test suite
# ===========================================================================

def test_sprt_boundaries() -> None:
    """SPRTState boundaries should be ±ln((1-β)/α)."""
    print("\n--- SPRT unit tests ---")
    s = SPRTState(elo0=0, elo1=3, alpha=0.05, beta=0.05)
    expected_hi = math.log(0.95 / 0.05)
    expected_lo = math.log(0.05 / 0.95)
    result("boundaries correct",
           abs(s.hi - expected_hi) < 1e-9 and abs(s.lo - expected_lo) < 1e-9,
           f"lo={s.lo:.4f} hi={s.hi:.4f}")


def test_sprt_h1_converges() -> None:
    """Heavy win rate should push LLR to H1."""
    s = SPRTState(elo0=0, elo1=3)
    # ~70% score (500W / 100D / 100L) → definite H1
    for _ in range(500):
        s.update("win")
    for _ in range(100):
        s.update("draw")
    for _ in range(100):
        s.update("loss")
    verdict = s.conclusion()
    result("heavy wins -> H1 accepted", verdict == "H1_accepted",
           f"LLR={s.llr:+.3f} ({s.games} games)")


def test_sprt_h0_converges() -> None:
    """Clearly losing test engine should push LLR to H0."""
    s = SPRTState(elo0=0, elo1=3)
    # 0W / 50D / 500L  -> test engine well below H0 baseline
    for _ in range(50):
        s.update("draw")
    for _ in range(500):
        s.update("loss")
    verdict = s.conclusion()
    result("heavy losses -> H0 accepted", verdict == "H0_accepted",
           f"LLR={s.llr:+.3f} ({s.games} games)")


def test_sprt_zero_games() -> None:
    s = SPRTState()
    result("zero games LLR is 0", s.llr == 0.0)
    result("zero games inconclusive", s.conclusion() is None)


def test_sprt_update_invalid() -> None:
    s = SPRTState()
    try:
        s.update("blunder")
        result("invalid result raises ValueError", False)
    except ValueError:
        result("invalid result raises ValueError", True)


def test_sprt_summary_format() -> None:
    s = SPRTState(elo0=0, elo1=3)
    for _ in range(10):
        s.update("win")
    for _ in range(5):
        s.update("draw")
    summary = s.summary()
    ok = "LLR" in summary and "W:10" in summary and "D:5" in summary
    result("summary contains expected fields", ok, summary)


# ---------------------------------------------------------------------------
# Syzygy UCI tests (engine)
# ---------------------------------------------------------------------------

def test_syzygy_options_advertised() -> None:
    """Engine must advertise all three Syzygy options in 'uci' response."""
    print("\n--- Syzygy UCI integration tests ---")
    if not os.path.isfile(HCE_EXE):
        skip("syzygy options advertised", f"binary not found: {HCE_EXE}")
        return

    sess = UCISession(HCE_EXE)
    sess.send("uci", delay=0.05)
    lines = sess.collect("uciok", timeout=5)
    sess.quit()

    has_path  = any("SyzygyPath"        in l for l in lines)
    has_depth = any("SyzygyProbeDepth"  in l for l in lines)
    has_50    = any("Syzygy50MoveRule"  in l for l in lines)
    result("SyzygyPath option present",       has_path)
    result("SyzygyProbeDepth option present", has_depth)
    result("Syzygy50MoveRule option present", has_50)


def test_syzygy_empty_path_no_crash() -> None:
    """Setting SyzygyPath to <empty> must not crash the engine."""
    if not os.path.isfile(HCE_EXE):
        skip("empty path no crash")
        return

    sess = UCISession(HCE_EXE)
    sess.send("uci", delay=0.1)
    sess.collect("uciok", timeout=5)
    sess.send("setoption name SyzygyPath value <empty>", delay=0.1)
    sess.send("isready", delay=0.1)
    lines = sess.collect("readyok", timeout=5)
    ok = any("readyok" in l for l in lines) and sess._p.poll() is None
    sess.quit()
    result("empty SyzygyPath no crash", ok)


def test_syzygy_nonexistent_path_no_crash() -> None:
    """Setting SyzygyPath to a nonexistent dir must not crash."""
    if not os.path.isfile(HCE_EXE):
        skip("nonexistent path no crash")
        return

    sess = UCISession(HCE_EXE)
    sess.send("uci", delay=0.1)
    sess.collect("uciok", timeout=5)
    sess.send("setoption name SyzygyPath value C:\\does\\not\\exist", delay=0.1)
    sess.send("isready", delay=0.1)
    lines = sess.collect("readyok", timeout=5)
    ok = any("readyok" in l for l in lines) and sess._p.poll() is None
    sess.quit()
    result("nonexistent SyzygyPath no crash", ok)


def test_syzygy_probe_depth_set() -> None:
    """SyzygyProbeDepth setoption + search must not crash."""
    if not os.path.isfile(HCE_EXE):
        skip("probe depth set")
        return

    sess = UCISession(HCE_EXE)
    sess.send("uci", delay=0.1)
    sess.collect("uciok", timeout=5)
    sess.send("setoption name SyzygyProbeDepth value 4", delay=0.05)
    sess.send("isready", delay=0.1)
    sess.collect("readyok", timeout=5)
    # Run a short search
    sess.send("position startpos", delay=0.05)
    sess.send("go movetime 100", delay=0.1)
    lines = sess.collect("bestmove", timeout=8)
    ok = any("bestmove" in l for l in lines) and sess._p.poll() is None
    sess.quit()
    result("SyzygyProbeDepth set + search returns bestmove", ok)


def test_syzygy_50move_rule_toggle() -> None:
    """Syzygy50MoveRule can be toggled without crash."""
    if not os.path.isfile(HCE_EXE):
        skip("50move rule toggle")
        return

    sess = UCISession(HCE_EXE)
    sess.send("uci", delay=0.1)
    sess.collect("uciok", timeout=5)
    sess.send("setoption name Syzygy50MoveRule value false", delay=0.05)
    sess.send("setoption name Syzygy50MoveRule value true",  delay=0.05)
    sess.send("isready", delay=0.1)
    lines = sess.collect("readyok", timeout=5)
    ok = any("readyok" in l for l in lines) and sess._p.poll() is None
    sess.quit()
    result("Syzygy50MoveRule toggle no crash", ok)


def test_syzygy_with_tb_files(tb_path: str) -> None:
    """
    With real TB files: probe a known KPK position.
    K+P vs K — white pawn on e7, white king d6, black king d8.
    FEN: 3k4/4P3/3K4/8/8/8/8/8 w - - 0 1  → White wins (TB_WIN).

    The engine should return a score above VALUE_TB_LOSS and quickly find
    a winning move (Kc7 or Ke7 or e8=Q).
    """
    print(f"\n--- Syzygy probe test (TB path: {tb_path}) ---")
    if not os.path.isfile(HCE_EXE):
        skip("TB probe KPK")
        return

    sess = UCISession(HCE_EXE, timeout=10)
    sess.send("uci", delay=0.1)
    sess.collect("uciok", timeout=5)
    sess.send(f"setoption name SyzygyPath value {tb_path}", delay=0.1)
    sess.send("setoption name SyzygyProbeDepth value 1", delay=0.05)
    sess.send("isready", delay=0.1)
    lines = sess.collect("readyok", timeout=5)

    if not any("readyok" in l for l in lines):
        result("TB probe KPK: readyok received", False, "engine did not respond")
        sess.quit()
        return

    # KPK: white to move wins
    sess.send("position fen 3k4/4P3/3K4/8/8/8/8/8 w - - 0 1", delay=0.05)
    sess.send("go movetime 500", delay=0.1)
    lines = sess.collect("bestmove", timeout=8)

    bestmove_lines = [l for l in lines if "bestmove" in l]
    info_lines     = [l for l in lines if "info" in l and "score" in l]

    has_bestmove = bool(bestmove_lines)
    # Score should be large positive (TB win range ~31800)
    score_ok = False
    for l in info_lines:
        parts = l.split()
        if "score" in parts:
            idx = parts.index("score")
            if idx + 2 < len(parts) and parts[idx + 1] == "cp":
                try:
                    cp = int(parts[idx + 2])
                    if cp > 20000:  # well above normal eval, indicates TB hit
                        score_ok = True
                except ValueError:
                    pass
            if parts[idx + 1] == "mate":
                score_ok = True  # mate score also acceptable

    result("TB probe KPK: bestmove returned", has_bestmove,
           bestmove_lines[0] if bestmove_lines else "no bestmove")
    result("TB probe KPK: score indicates TB win", score_ok,
           info_lines[-1] if info_lines else "no info line")

    # KPK: black to move should be losing
    sess.send("position fen 3k4/4P3/3K4/8/8/8/8/8 b - - 0 1", delay=0.05)
    sess.send("go movetime 500", delay=0.1)
    b_lines = sess.collect("bestmove", timeout=8)
    b_info  = [l for l in b_lines if "info" in l and "score" in l]
    b_score_negative = False
    for l in b_info:
        parts = l.split()
        if "score" in parts:
            idx = parts.index("score")
            if idx + 2 < len(parts) and parts[idx + 1] == "cp":
                try:
                    cp = int(parts[idx + 2])
                    if cp < -20000:
                        b_score_negative = True
                except ValueError:
                    pass
            if parts[idx + 1] == "mate":
                # negative mate = being mated
                try:
                    mate_n = int(parts[idx + 2])
                    if mate_n < 0:
                        b_score_negative = True
                except ValueError:
                    pass
    result("TB probe KPK: black score is negative (losing)", b_score_negative,
           b_info[-1] if b_info else "no info line")

    sess.quit()


def test_sprt_tune_help() -> None:
    """sprt_tune.py --help must exit 0 and mention expected flags."""
    print("\n--- sprt_tune.py smoke test ---")
    script = os.path.join(TOOLS_DIR, "sprt_tune.py")
    if not os.path.isfile(script):
        skip("sprt_tune --help")
        return
    r = subprocess.run(
        [sys.executable, script, "--help"],
        capture_output=True, text=True, timeout=10
    )
    ok = r.returncode == 0 and "--base" in r.stdout and "--elo1" in r.stdout
    result("sprt_tune.py --help exit 0", ok, r.stdout[:120].replace("\n", " "))


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tb-path", default=None,
                        help="Path to Syzygy tablebase directory (enables live probe tests)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Syzygy + SPRT verification tests")
    print("=" * 60)

    # SPRT unit tests
    test_sprt_boundaries()
    test_sprt_h1_converges()
    test_sprt_h0_converges()
    test_sprt_zero_games()
    test_sprt_update_invalid()
    test_sprt_summary_format()

    # Syzygy engine integration tests (no TB files needed)
    test_syzygy_options_advertised()
    test_syzygy_empty_path_no_crash()
    test_syzygy_nonexistent_path_no_crash()
    test_syzygy_probe_depth_set()
    test_syzygy_50move_rule_toggle()

    # sprt_tune.py smoke
    test_sprt_tune_help()

    # Live TB probe (only if --tb-path given)
    if args.tb_path:
        test_syzygy_with_tb_files(args.tb_path)
    else:
        print(f"\n  [{SKIP}] TB live probe tests  (pass --tb-path to enable)")

    # Summary
    print()
    print("=" * 60)
    passes = sum(1 for _, s, _ in _results if s == "PASS")
    fails  = sum(1 for _, s, _ in _results if s == "FAIL")
    skips  = sum(1 for _, s, _ in _results if s == "SKIP")
    total  = passes + fails + skips
    print(f"  Results: {passes}/{total} passed, {fails} failed, {skips} skipped")
    print("=" * 60)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
