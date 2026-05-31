"""Test HCE binary — capture ALL output and exit code."""
import subprocess, time, threading, sys, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
EXE = os.path.join(ROOT, "bot", "engine", "redux-hce.exe")
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
    except Exception as e:
        print(f"[SEND ERROR] {cmd}: {e}")
    time.sleep(wait)

timer = threading.Timer(20.0, lambda: (print("\n!!! HARD TIMEOUT !!!"), os._exit(1)))
timer.daemon = True
timer.start()

send('uci', 0.5)
send('setoption name Threads value 8', 0.2)
send('setoption name Hash value 512', 0.2)
send('setoption name UseNNUE value false', 0.2)
send('isready', 1.0)
send('position fen rnbqkb1r/pppppppp/5n2/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 2 2', 0.2)
send('go movetime 5000')

# Wait up to 12s
deadline = time.time() + 12
got_bestmove = False
while time.time() < deadline:
    time.sleep(0.3)
    if p.poll() is not None:
        print(f"[ENGINE DIED] exit code = {p.returncode}")
        break
    for _, _, line in lines:
        if 'bestmove' in line:
            got_bestmove = True
    if got_bestmove:
        break

send('quit', 0.3)
try: p.kill()
except: pass

print(f"\n=== ALL OUTPUT ===")
for tag, ts, line in lines:
    print(f"[{tag}] {line}")

print(f"\nbest_move received: {got_bestmove}")
print(f"process alive: {p.poll() is None}")
if p.returncode is not None:
    print(f"exit code: {p.returncode}")
