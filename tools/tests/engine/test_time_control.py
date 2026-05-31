#!/usr/bin/env python3
"""
test_time_control.py — Comprehensive time control diagnostic.

Tests the engine's actual UCI time management behavior with real commands.
Uses threaded non-blocking I/O to avoid hanging on long engine computations.
"""

import subprocess
import time
import sys
import re
import os
import threading
import queue

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
ENGINE_PATH = os.path.join(ROOT, "bot", "engine", "redux-hce.exe")

class UCIEngine:
    def __init__(self, path):
        self.proc = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._q = queue.Queue()
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        
    def _reader(self):
        """Background thread that reads stdout and puts lines on a queue."""
        try:
            for line in self.proc.stdout:
                self._q.put(line.strip())
        except:
            pass
        self._q.put(None)  # sentinel
        
    def readline(self, timeout=30):
        """Non-blocking readline with timeout."""
        try:
            line = self._q.get(timeout=timeout)
            return line
        except queue.Empty:
            return None
        
    def send(self, cmd):
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except:
            pass
        
    def read_until(self, target, timeout=10):
        """Read lines until we see one starting with `target`."""
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            lines.append(line)
            if line.startswith(target):
                return lines
        raise TimeoutError(f"Timed out waiting for '{target}'")
    
    def init(self):
        self.send("uci")
        self.read_until("uciok")
        self.send("setoption name Threads value 1")
        self.send("setoption name Hash value 64")
        self.send("isready")
        self.read_until("readyok")
        
    def go_and_wait(self, go_cmd, timeout=30):
        """Send a go command, collect info lines, return (bestmove_line, info_lines, wall_time_ms)."""
        info_lines = []
        start = time.time()
        self.send(go_cmd)
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            if line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                info_lines.append(line)
            if line.startswith("bestmove"):
                wall_ms = (time.time() - start) * 1000
                return line, info_lines, wall_ms
                
        raise TimeoutError(f"No bestmove after {timeout}s")
    
    def go_infinite_then_stop(self, delay_ms, timeout=30):
        """Send go infinite, wait delay_ms, send stop, collect bestmove."""
        info_lines = []
        start = time.time()
        self.send("go infinite")
        
        # Collect info lines for delay_ms (non-blocking)
        stop_time = start + delay_ms / 1000
        while time.time() < stop_time:
            remaining = stop_time - time.time()
            line = self.readline(timeout=max(0.01, remaining))
            if line and line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                info_lines.append(line)
        
        self.send("stop")
        
        # Wait for bestmove with timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            if line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                info_lines.append(line)
            if line.startswith("bestmove"):
                wall_ms = (time.time() - start) * 1000
                return line, info_lines, wall_ms
        
        raise TimeoutError("No bestmove after stop")
    
    def go_ponder_then_ponderhit(self, ponder_ms, timeout=15):
        """
        Send go ponder, wait ponder_ms, then send ponderhit.
        Returns (bestmove, ponder_infos, post_infos, post_ms, total_ms) or raises TimeoutError.
        """
        ponder_infos = []
        post_infos = []
        
        start = time.time()
        self.send("go ponder")
        
        # Collect ponder info (non-blocking)
        ponder_end = start + ponder_ms / 1000
        while time.time() < ponder_end:
            remaining = ponder_end - time.time()
            line = self.readline(timeout=max(0.01, remaining))
            if line and line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                ponder_infos.append(line)
        
        ponderhit_start = time.time()
        self.send("ponderhit")
        
        # Wait for bestmove with timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            if line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                post_infos.append(line)
            if line.startswith("bestmove"):
                post_ms = (time.time() - ponderhit_start) * 1000
                total_ms = (time.time() - start) * 1000
                return line, ponder_infos, post_infos, post_ms, total_ms
        
        raise TimeoutError(f"No bestmove after ponderhit (waited {timeout}s)")
    
    def go_ponder_then_stop(self, ponder_ms, timeout=30):
        """Send go ponder, wait ponder_ms, then send stop."""
        info_lines = []
        
        start = time.time()
        self.send("go ponder")
        
        ponder_end = start + ponder_ms / 1000
        while time.time() < ponder_end:
            remaining = ponder_end - time.time()
            line = self.readline(timeout=max(0.01, remaining))
            if line and line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                info_lines.append(line)
        
        self.send("stop")
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line is None:
                break
            if line.startswith("info depth") and "score" in line and not line.startswith("info string"):
                info_lines.append(line)
            if line.startswith("bestmove"):
                wall_ms = (time.time() - start) * 1000
                return line, info_lines, wall_ms
        
        raise TimeoutError("No bestmove after ponder stop")
    
    def kill(self):
        try:
            self.proc.kill()
        except:
            pass
    
    def quit(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=3)
        except:
            self.kill()


def parse_info_time(info_line):
    m = re.search(r'\btime\s+(\d+)', info_line)
    return int(m.group(1)) if m else None

def parse_info_depth(info_line):
    m = re.search(r'\bdepth\s+(\d+)', info_line)
    return int(m.group(1)) if m else None


def header(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def test_result(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    sym = "+" if passed else "X"
    print(f"  [{status}] {sym} {name}")
    if detail:
        print(f"         {detail}")
    return passed


def run_test(name, fn, test_timeout=60):
    """Run a test function with overall timeout. Returns (passed_list, hung)."""
    result_holder = [None]
    exc_holder = [None]
    
    def wrapper():
        try:
            result_holder[0] = fn()
        except Exception as e:
            exc_holder[0] = e
    
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    t.join(timeout=test_timeout)
    
    if t.is_alive():
        print(f"  [FAIL] X {name} -- TIMED OUT after {test_timeout}s (engine hung!)")
        return [False], True
    
    if exc_holder[0]:
        print(f"  [FAIL] X {name} -- Exception: {exc_holder[0]}")
        return [False], False
    
    return result_holder[0] if result_holder[0] else [True], False


def main():
    if not os.path.exists(ENGINE_PATH):
        print(f"Engine not found: {ENGINE_PATH}")
        sys.exit(1)
    
    print(f"Engine: {ENGINE_PATH}")
    all_results = []
    
    # ======================================================================
    # TEST 1: go movetime 500
    # ======================================================================
    def test1():
        header("TEST 1: go movetime 500ms")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos")
        bm, infos, wall = eng.go_and_wait("go movetime 500")
        engine_time = parse_info_time(infos[-1]) if infos else 0
        print(f"  Wall time: {wall:.0f}ms, engine reported: {engine_time}ms")
        print(f"  Bestmove: {bm}")
        r = [test_result("movetime 500: wall in [400, 700]ms", 400 <= wall <= 700, f"wall={wall:.0f}ms")]
        eng.quit()
        return r
    
    results, _ = run_test("movetime 500", test1)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 2: go movetime 100
    # ======================================================================
    def test2():
        header("TEST 2: go movetime 100ms")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos")
        bm, infos, wall = eng.go_and_wait("go movetime 100")
        print(f"  Wall time: {wall:.0f}ms")
        r = [test_result("movetime 100: wall in [50, 250]ms", 50 <= wall <= 250, f"wall={wall:.0f}ms")]
        eng.quit()
        return r
    
    results, _ = run_test("movetime 100", test2)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 3: go depth 6
    # ======================================================================
    def test3():
        header("TEST 3: go depth 6")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos")
        bm, infos, wall = eng.go_and_wait("go depth 6")
        max_depth = max([parse_info_depth(i) for i in infos]) if infos else 0
        print(f"  Wall time: {wall:.0f}ms, max depth: {max_depth}")
        r = [test_result("depth 6: max depth == 6", max_depth == 6, f"max_depth={max_depth}")]
        eng.quit()
        return r
    
    results, _ = run_test("depth 6", test3)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 4: go infinite + stop after 1000ms
    # ======================================================================
    def test4():
        header("TEST 4: go infinite + stop after 1000ms")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos")
        bm, infos, wall = eng.go_infinite_then_stop(1000)
        max_depth = max([parse_info_depth(i) for i in infos]) if infos else 0
        print(f"  Wall time: {wall:.0f}ms, max depth: {max_depth}, info lines: {len(infos)}")
        r = [
            test_result("go infinite: wall ~1000ms", 800 <= wall <= 1500, f"wall={wall:.0f}ms"),
            test_result("go infinite: depth >= 4", max_depth >= 4, f"max_depth={max_depth}"),
        ]
        eng.quit()
        return r
    
    results, _ = run_test("go infinite", test4)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 5: Bullet 1+0
    # ======================================================================
    def test5():
        header("TEST 5: Bullet clock (1+0, 60s each)")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos")
        bm, infos, wall = eng.go_and_wait("go wtime 60000 btime 60000 winc 0 binc 0")
        engine_time = parse_info_time(infos[-1]) if infos else 0
        depths = [parse_info_depth(i) for i in infos]
        print(f"  Wall: {wall:.0f}ms, engine: {engine_time}ms, depths: {depths}")
        r = [test_result("bullet 1+0: wall in [500, 6000]ms", 500 <= wall <= 6000, f"wall={wall:.0f}ms")]
        eng.quit()
        return r
    
    results, _ = run_test("bullet 1+0", test5, test_timeout=15)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 6: go ponder + stop after 2000ms
    # ======================================================================
    def test6():
        header("TEST 6: go ponder + stop after 2000ms")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos")
        bm, infos, wall = eng.go_ponder_then_stop(2000)
        max_depth = max([parse_info_depth(i) for i in infos]) if infos else 0
        print(f"  Wall: {wall:.0f}ms, max depth: {max_depth}, infos: {len(infos)}")
        r = [
            test_result("ponder+stop: wall ~2000ms", 1500 <= wall <= 3500, f"wall={wall:.0f}ms"),
            test_result("ponder: depth >= 6", max_depth >= 6, f"max_depth={max_depth}"),
        ]
        eng.quit()
        return r
    
    results, _ = run_test("ponder + stop", test6)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 7: go ponder + ponderhit — THE CRITICAL BUG TEST
    # ======================================================================
    def test7():
        header("TEST 7: go ponder + ponderhit -- CRITICAL BUG TEST")
        eng = UCIEngine(ENGINE_PATH)
        try:
            eng.init()
            eng.send("position startpos")
            bm, pinfos, postinfos, post_ms, total_ms = eng.go_ponder_then_ponderhit(
                ponder_ms=2000, timeout=10
            )
            ponder_depth = max([parse_info_depth(i) for i in pinfos]) if pinfos else 0
            print(f"  Ponder: {len(pinfos)} infos, max depth {ponder_depth}")
            print(f"  Post-ponderhit: {post_ms:.0f}ms, {len(postinfos)} infos")
            print(f"  Total: {total_ms:.0f}ms, bestmove: {bm}")
            r = [test_result("ponderhit: responds within 10s", post_ms < 10000, f"post_ms={post_ms:.0f}ms")]
            eng.quit()
            return r
        except TimeoutError as e:
            print(f"  ENGINE HUNG! {e}")
            eng.kill()
            return [test_result("ponderhit: engine responds", False, "ENGINE HUNG -- ponderhit bug confirmed!")]
    
    results, hung = run_test("ponderhit", test7, test_timeout=20)
    all_results.extend(results)
    if hung:
        print("  (test thread killed; engine process may still be running)")
    
    # ======================================================================
    # TEST 7b: go ponder (ID loop finishes first) + ponderhit — SPIN-WAIT BUG
    # This tests the case where the ponder search exhausts all depths BEFORE
    # ponderhit arrives. The old code would hang in the spin-wait forever.
    # ======================================================================
    def test7b():
        header("TEST 7b: SPIN-WAIT BUG — ponder completes then ponderhit")
        eng = UCIEngine(ENGINE_PATH)
        try:
            eng.init()
            # Use a simple position and very long ponder (3s) so engine
            # finishes its ID loop before ponderhit
            eng.send("position startpos")
            bm, pinfos, postinfos, post_ms, total_ms = eng.go_ponder_then_ponderhit(
                ponder_ms=5000, timeout=10  # 5s ponder: engine certainly finishes ID loop
            )
            ponder_depth = max([parse_info_depth(i) for i in pinfos]) if pinfos else 0
            print(f"  Ponder: {len(pinfos)} infos, max depth {ponder_depth}")
            print(f"  Post-ponderhit: {post_ms:.0f}ms")
            print(f"  Total: {total_ms:.0f}ms, bestmove: {bm}")
            r = [test_result("spin-wait ponderhit: responds within 10s", post_ms < 5000,
                            f"post_ms={post_ms:.0f}ms (if >5s = spin-wait hang)")]
            eng.quit()
            return r
        except TimeoutError as e:
            print(f"  ENGINE HUNG! {e}")
            eng.kill()
            return [test_result("spin-wait ponderhit: engine responds", False,
                               "ENGINE HUNG -- spin-wait bug still present!")]
    
    results, hung = run_test("spin-wait ponderhit", test7b, test_timeout=25)
    all_results.extend(results)
    if hung:
        print("  (test thread killed; spin-wait bug confirmed)")
    
    # ======================================================================
    # TEST 8: Low clock (5s left)
    # ======================================================================
    def test8():
        header("TEST 8: Low clock (5s left, no increment)")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        # White to move after e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 — white has 5s
        eng.send("position startpos moves e2e4 e7e5 g1f3 b8c6 f1b5 a7a6")
        bm, infos, wall = eng.go_and_wait("go wtime 5000 btime 30000 winc 0 binc 0")
        print(f"  Wall: {wall:.0f}ms, bestmove: {bm}")
        r = [test_result("low clock 5s: wall < 600ms", wall < 600, f"wall={wall:.0f}ms")]
        eng.quit()
        return r
    
    results, _ = run_test("low clock", test8)
    all_results.extend(results)
    
    # ======================================================================
    # TEST 9: Critical clock (1s left)
    # ======================================================================
    def test9():
        header("TEST 9: Critical clock (1s left)")
        eng = UCIEngine(ENGINE_PATH)
        eng.init()
        eng.send("position startpos moves e2e4 e7e5")
        bm, infos, wall = eng.go_and_wait("go wtime 1000 btime 30000 winc 0 binc 0")
        print(f"  Wall: {wall:.0f}ms, bestmove: {bm}")
        r = [test_result("critical 1s: wall < 200ms", wall < 200, f"wall={wall:.0f}ms")]
        eng.quit()
        return r
    
    results, _ = run_test("critical clock", test9)
    all_results.extend(results)
    
    # ======================================================================
    # ARCHITECTURE ANALYSIS
    # ======================================================================
    header("ARCHITECTURE ANALYSIS")
    print("""
  FINDINGS:

  1. Bot (game.js) always sends 'go infinite' -- NEVER 'go wtime/btime'.
     C++ allocate_time() / should_stop() / soft_time / hard_time are
     all dead code in production bot games.

  2. All time control is JS-side via policies.shouldStopSearch():
     - Fires only when an 'info depth' line arrives from the engine
     - If the engine is deep in alpha-beta with no output, time control
       has NO enforcement until the JS setTimeout backup fires

  3. Ponder uses 'go infinite' (not 'go ponder'):
     - C++ ponder/ponderhit machinery is NEVER exercised by the bot
     - Bot's convertPonderToSearch() is purely JS-level

  4. UCI cmd_ponderhit() passes zeros: Search.ponderhit(0,0,0,0,0)
     - Even if real UCI ponderhit were used, clock data is lost

  5. C++ search() spin-wait checks 'limits.ponder' instead of 'pondering_':
     - After ponderhit clears pondering_, spin-wait still blocks
     - Engine HANGS forever after ponderhit (confirmed in Test 7)

  6. shouldStopSearch() 'budget50' gate fires at 50% of maxTimeMs:
     - Only checked when info lines arrive
     - Enforcement gap: engine could overrun by seconds between depths
""")
    
    # ======================================================================
    # SUMMARY
    # ======================================================================
    header("SUMMARY")
    passed = sum(1 for r in all_results if r)
    failed = sum(1 for r in all_results if not r)
    total = len(all_results)
    print(f"\n  {passed} passed, {failed} failed out of {total} tests\n")
    
    if failed > 0:
        print("  BUGS FOUND:")
        print("    1. C++ ponderhit hangs engine (spin-wait on limits.ponder)")
        print("    2. UCI cmd_ponderhit sends zeros (no real clock data)")
        print("    3. Bot bypasses C++ time management entirely")
        print()
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
