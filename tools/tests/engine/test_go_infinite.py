"""Test go infinite + stop (the pattern the bot uses)."""
import subprocess, threading, time, sys, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
ENGINE = os.path.join(ROOT, "bot", "engine", "redux-hce.exe")

def test_go_infinite(threads, stop_after_ms=3000):
    proc = subprocess.Popen(
        [ENGINE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )
    def send(cmd):
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()

    send("uci")
    time.sleep(0.3)
    send(f"setoption name Threads value {threads}")
    send(f"setoption name Hash value 64")
    send("isready")
    time.sleep(0.3)
    send("ucinewgame")
    send("position startpos")
    send("go infinite")

    bestmove = None
    max_depth = 0
    lines = []

    def reader():
        nonlocal bestmove, max_depth
        for line in proc.stdout:
            line = line.strip()
            lines.append(line)
            if "info depth" in line and "info string" not in line:
                try:
                    d = int(line.split("info depth")[1].split()[0])
                    max_depth = max(max_depth, d)
                except:
                    pass
            if line.startswith("bestmove"):
                bestmove = line
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Wait, then send stop
    time.sleep(stop_after_ms / 1000.0)
    try:
        send("stop")
    except:
        pass

    t.join(timeout=5.0)

    rc = proc.poll()
    alive = rc is None
    if alive:
        try:
            send("quit")
            proc.wait(timeout=2)
        except:
            proc.kill()
        rc = proc.returncode

    return bestmove, max_depth, rc, alive

print("=== go infinite + stop test ===")
for threads in [1, 2, 4, 8]:
    bm, depth, rc, alive = test_go_infinite(threads, stop_after_ms=3000)
    status = "OK" if bm else ("CRASH" if rc == 3221225477 else "NO_BESTMOVE")
    print(f"  {threads}T: {status} depth={depth} rc={rc} bestmove={bm}")
    sys.stdout.flush()
