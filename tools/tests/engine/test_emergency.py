"""Quick test: emergency movetime + ponderhit scenarios."""
import subprocess, time, threading, queue, sys, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
ENGINE = os.path.join(ROOT, "bot", "engine", "redux-hce.exe")

class E:
    def __init__(self):
        self.proc = subprocess.Popen(
            [ENGINE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=0)
        self.q = queue.Queue()
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()
    def _read(self):
        for line in self.proc.stdout:
            self.q.put(line.strip())
    def send(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
    def readline(self, timeout=10):
        try: return self.q.get(timeout=timeout)
        except queue.Empty: return None
    def init(self):
        self.send("uci")
        while True:
            l = self.readline(5)
            if l and "uciok" in l: break
        self.send("isready")
        while True:
            l = self.readline(5)
            if l and "readyok" in l: break
    def quit(self):
        try:
            self.send("quit")
            self.proc.wait(2)
        except: self.proc.kill()

def test_movetime(ms_val):
    """Test go movetime N — engine should stop around N ms."""
    eng = E()
    eng.init()
    eng.send("position startpos")
    t0 = time.time()
    eng.send(f"go movetime {ms_val}")
    while True:
        l = eng.readline(ms_val/1000 + 5)
        if l and l.startswith("bestmove"):
            wall = (time.time() - t0) * 1000
            eng.quit()
            return wall
    eng.quit()
    return -1

def test_ponderhit_with_clock():
    """Test go ponder then ponderhit with real clocks (60s)."""
    eng = E()
    eng.init()
    eng.send("position startpos")
    eng.send("go ponder")
    time.sleep(2)
    t0 = time.time()
    eng.send("ponderhit wtime 60000 btime 60000 winc 0 binc 0")
    while True:
        l = eng.readline(15)
        if l and l.startswith("bestmove"):
            wall = (time.time() - t0) * 1000
            eng.quit()
            return wall
    eng.quit()
    return -1

def test_ponderhit_with_zeros():
    """Test go ponder then bare ponderhit (no clocks — fallback to saved_limits)."""
    eng = E()
    eng.init()
    eng.send("position startpos")
    eng.send("go ponder")
    time.sleep(2)
    t0 = time.time()
    eng.send("ponderhit")
    while True:
        l = eng.readline(15)
        if l and l.startswith("bestmove"):
            wall = (time.time() - t0) * 1000
            eng.quit()
            return wall
    eng.quit()
    return -1

if __name__ == "__main__":
    print("=" * 60)
    print("  TEST 1: go movetime 50 (emergency: ~50ms)")
    print("=" * 60)
    wall = test_movetime(50)
    ok = 20 <= wall <= 200
    print(f"  Wall: {wall:.0f}ms  {'PASS' if ok else 'FAIL'}")

    print()
    print("=" * 60)
    print("  TEST 2: go movetime 20 (extreme emergency: ~20ms)")
    print("=" * 60)
    wall = test_movetime(20)
    ok = 10 <= wall <= 150
    print(f"  Wall: {wall:.0f}ms  {'PASS' if ok else 'FAIL'}")

    print()
    print("=" * 60)
    print("  TEST 3: go movetime 100 (moderate emergency: ~100ms)")
    print("=" * 60)
    wall = test_movetime(100)
    ok = 50 <= wall <= 250
    print(f"  Wall: {wall:.0f}ms  {'PASS' if ok else 'FAIL'}")

    print()
    print("=" * 60)
    print("  TEST 4: ponderhit with 60s clock")
    print("=" * 60)
    wall = test_ponderhit_with_clock()
    ok = wall < 5000
    print(f"  Post-ponderhit: {wall:.0f}ms  {'PASS' if ok else 'FAIL'}")

    print()
    print("=" * 60)
    print("  TEST 5: ponderhit with zeros (saved_limits fallback)")
    print("=" * 60)
    wall = test_ponderhit_with_zeros()
    ok = wall < 5000
    print(f"  Post-ponderhit: {wall:.0f}ms  {'PASS' if ok else 'FAIL'}")

    print()
    print("Done.")
