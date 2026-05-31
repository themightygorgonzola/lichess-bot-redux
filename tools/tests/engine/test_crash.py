"""Narrowing test: vary threads and depth to find crash threshold."""
import subprocess, time, threading, sys, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
EXE = os.path.join(ROOT, "bot", "engine", "redux-hce.exe")

def test(threads, depth_limit=None, movetime=5000, timeout=15):
    lines = []
    def reader(pipe, tag):
        try:
            for line in iter(pipe.readline, ''):
                lines.append((tag, line.rstrip('\r\n')))
        except: pass
    
    p = subprocess.Popen([EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    threading.Thread(target=reader, args=(p.stdout, 'O'), daemon=True).start()
    threading.Thread(target=reader, args=(p.stderr, 'E'), daemon=True).start()
    
    def send(c, w=0.2):
        try: p.stdin.write(c+'\n'); p.stdin.flush()
        except: pass
        time.sleep(w)
    
    send('uci', 0.5)
    send(f'setoption name Threads value {threads}', 0.2)
    send('setoption name Hash value 512', 0.2)
    send('isready', 0.5)
    send('position fen rnbqkb1r/pppppppp/5n2/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 2 2', 0.2)
    if depth_limit:
        send(f'go depth {depth_limit}')
    else:
        send(f'go movetime {movetime}')
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        if p.poll() is not None:
            break
        for _, l in lines:
            if 'bestmove' in l:
                send('quit', 0.2)
                try: p.kill()
                except: pass
                max_d = 0
                for _, l2 in lines:
                    if 'info depth' in l2:
                        try: max_d = max(max_d, int(l2.split('info depth')[1].split()[0]))
                        except: pass
                return f"PASS depth={max_d}", p.returncode
    
    send('quit', 0.2)
    try: p.kill()
    except: pass
    rc = p.wait(timeout=2)
    max_d = 0
    for _, l in lines:
        if 'info depth' in l:
            try: max_d = max(max_d, int(l.split('info depth')[1].split()[0]))
            except: pass
    if rc and rc < 0 or rc == 3221225477:
        return f"CRASH depth={max_d}", rc
    return f"TIMEOUT depth={max_d}", rc

timer = threading.Timer(90.0, lambda: os._exit(1))
timer.daemon = True
timer.start()

configs = [
    (2, 7), (2, 8), (2, 10),
    (4, 7), (4, 8), (4, 10),
    (8, 7), (8, 8), (8, 10),
    (8, None),  # movetime test
]

for threads, depth in configs:
    label = f"{threads}T depth={'mt5s' if depth is None else depth}"
    result, rc = test(threads, depth)
    print(f"{label:20s} -> {result} (exit={rc})")
    sys.stdout.flush()
