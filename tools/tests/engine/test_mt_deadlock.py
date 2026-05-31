"""Test engine with 2 threads to find deadlock."""
import subprocess, time, threading, queue, sys, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
exe = os.path.join(ROOT, "bot", "engine", "redux-nnue.exe")
env = {"REDUX_TRACE_SEARCH": "true"}
import os
full_env = {**os.environ, **env}

proc = subprocess.Popen(
    [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1, env=full_env
)

qout = queue.Queue()
qerr = queue.Queue()

def read_out():
    for line in proc.stdout:
        qout.put(line.rstrip())

def read_err():
    for line in proc.stderr:
        qerr.put(line.rstrip())

threading.Thread(target=read_out, daemon=True).start()
threading.Thread(target=read_err, daemon=True).start()

def send(cmd):
    proc.stdin.write(cmd + "\n")
    proc.stdin.flush()

def drain(timeout=1.5):
    lines = []
    end = time.time() + timeout
    while True:
        rem = end - time.time()
        if rem <= 0:
            break
        try:
            lines.append(qout.get(timeout=rem))
        except queue.Empty:
            break
    return lines

# Setup
send("uci")
drain(1)
send("setoption name Threads value 2")
send("setoption name Hash value 64")
send("setoption name UseNNUE value false")
send("isready")
r = drain(1)
print("readyok:", any("readyok" in l for l in r))

send("ucinewgame")
send("position startpos")
send("go infinite")
print("--- go infinite, waiting 8s ---")
time.sleep(8)

# Collect trace
errs = []
while True:
    try:
        errs.append(qerr.get_nowait())
    except queue.Empty:
        break

print(f"--- last 60 of {len(errs)} trace lines ---")
for l in errs[-60:]:
    print(l)

# Collect info lines
outs = []
while True:
    try:
        outs.append(qout.get_nowait())
    except queue.Empty:
        break

print("--- stdout info lines ---")
for l in outs:
    if "info depth" in l and "score" in l:
        print(l[:120])

send("stop")
time.sleep(2)

# Post-stop output
while True:
    try:
        l = qout.get_nowait()
        print("POST:", l[:120])
    except queue.Empty:
        break

send("quit")
try:
    proc.wait(timeout=3)
except subprocess.TimeoutExpired:
    print("Engine didn't quit, killing")
    proc.kill()

print("Done")
