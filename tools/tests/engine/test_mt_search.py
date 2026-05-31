#!/usr/bin/env python3
"""Quick MT search test: 2 threads, 8s search, check depth progression.

Uses signal-based hard timeout to ensure the script never hangs.
"""
import subprocess, time, threading, sys, os, signal

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
EXE = os.path.join(ROOT, "bot", "engine", "redux-hce.exe")

def run_test(threads=2, movetime_ms=8000, timeout_s=20):
    lines = []

    def reader(pipe, tag):
        try:
            for line in iter(pipe.readline, ''):
                lines.append((tag, time.time(), line.rstrip('\r\n')))
        except: pass

    p = subprocess.Popen(
        [EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1
    )
    t1 = threading.Thread(target=reader, args=(p.stdout, 'O'), daemon=True)
    t2 = threading.Thread(target=reader, args=(p.stderr, 'E'), daemon=True)
    t1.start(); t2.start()

    def send(cmd, wait=0.2):
        try:
            p.stdin.write(cmd + '\n'); p.stdin.flush()
        except: pass
        time.sleep(wait)

    t0 = time.time()
    send('uci', 0.5)
    send('isready', 0.5)
    send(f'setoption name Threads value {threads}', 0.2)
    send('isready', 0.5)
    send('position fen rnbqkb1r/pppppppp/5n2/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 2 2', 0.2)
    send(f'go movetime {movetime_ms}')

    # Poll for bestmove with hard deadline
    deadline = time.time() + timeout_s
    got_bestmove = False
    while time.time() < deadline:
        time.sleep(0.3)
        for tag, ts, line in lines:
            if tag == 'O' and 'bestmove' in line:
                got_bestmove = True
        if got_bestmove:
            break

    if not got_bestmove:
        send('stop', 1.0)
        # Check again
        for tag, ts, line in lines:
            if tag == 'O' and 'bestmove' in line:
                got_bestmove = True

    send('quit', 0.3)
    try: p.kill()
    except: pass
    try: p.wait(timeout=2)
    except: pass

    elapsed = time.time() - t0
    print(f"\n=== MT-{threads} test completed in {elapsed:.1f}s ===")
    max_depth = 0
    for tag, ts, line in lines:
        if tag == 'O' and ('info depth' in line or 'bestmove' in line):
            print(line)
            if 'info depth' in line:
                try:
                    d = int(line.split('info depth')[1].split()[0])
                    max_depth = max(max_depth, d)
                except: pass
        elif tag == 'E' and line.strip():
            print(f'[ERR] {line}', file=sys.stderr)

    print(f"\nMax depth reached: {max_depth}")
    if max_depth >= 6:
        print("PASS - MT search progresses normally")
    elif max_depth >= 1:
        print(f"FAIL - MT search stuck (only reached depth {max_depth})")
    else:
        print("FAIL - No search output at all")
    return max_depth

if __name__ == '__main__':
    # Hard process timeout: kill self after 25s no matter what
    timer = threading.Timer(45.0, lambda: (print("\n!!! HARD TIMEOUT !!!"), os._exit(1)))
    timer.daemon = True
    timer.start()

    print("--- Test 1: 2 threads ---")
    d2 = run_test(threads=2, movetime_ms=8000, timeout_s=15)
    print("\n--- Test 2: 8 threads ---")
    d8 = run_test(threads=8, movetime_ms=8000, timeout_s=15)
    print(f"\nSummary: 2T depth={d2}, 8T depth={d8}")
