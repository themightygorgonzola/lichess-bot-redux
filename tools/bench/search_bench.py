#!/usr/bin/env python3
"""
search_bench.py â€” Wrapper for the engine's fixed-suite search NPS benchmark.

Runs the engine-side `--bench-search` command, optionally writes JSON output,
and uses the project's normal engine discovery logic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from engine_config import find_engine, find_nnue

DEFAULT_SUITE = ROOT / "data" / "benchmarks" / "search" / "nps_core.txt"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the engine's fixed-suite search benchmark")
    ap.add_argument("--engine", default="", help="Path to engine binary")
    ap.add_argument("--eval-file", default="", help="Optional nn.bin / exported eval file")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--hash", type=int, default=64, dest="hash_mb")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--warmup-depth", type=int, default=4)
    ap.add_argument("--mode", default="lazysmp", choices=["lazysmp", "rootsplit"])
    ap.add_argument("--suite-file", default="", help="Optional custom suite file: name|fen per line")
    ap.add_argument("--keep-tt", action="store_true")
    ap.add_argument("--keep-history", action="store_true")
    ap.add_argument("--profile", action="store_true", help="Collect structured hotspot profiling data")
    ap.add_argument("--json", action="store_true", help="Emit JSON from the engine")
    ap.add_argument("--output", default="", help="Optional path to write benchmark output")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="Hard timeout in seconds for engine benchmark execution")
    args = ap.parse_args()

    engine = find_engine(args.engine or None)
    eval_file = args.eval_file or find_nnue()

    cmd = [
        engine,
        "--bench-search",
        "--threads", str(args.threads),
        "--depth", str(args.depth),
        "--hash", str(args.hash_mb),
        "--repeat", str(args.repeat),
        "--warmup-depth", str(args.warmup_depth),
        "--mode", args.mode,
    ]
    if eval_file:
        cmd += ["--eval", eval_file]
    suite_file = args.suite_file.strip()
    if suite_file:
        cmd += ["--suite-file", suite_file]
    elif DEFAULT_SUITE.is_file():
        cmd += ["--suite-file", str(DEFAULT_SUITE)]
    if args.keep_tt:
        cmd += ["--keep-tt"]
    if args.keep_history:
        cmd += ["--keep-history"]
    if args.profile:
        cmd += ["--profile"]
    if args.json:
        cmd += ["--json"]

    proc_stdout = ""
    if args.json:
        if args.output:
            json_path = Path(args.output)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            cleanup_json = False
        else:
            tmp = tempfile.NamedTemporaryFile(prefix="search_bench_", suffix=".json", delete=False)
            json_path = Path(tmp.name)
            tmp.close()
            cleanup_json = True

        cmd += ["--json-file", str(json_path)]
        stderr_tmp = tempfile.NamedTemporaryFile(prefix="search_bench_", suffix=".stderr", delete=False)
        stderr_path = Path(stderr_tmp.name)
        stderr_tmp.close()

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        with open(stderr_path, "w", encoding="utf-8") as se:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=se,
                creationflags=creationflags,
                text=False,
            )

        deadline = time.monotonic() + max(0.1, args.timeout)
        timed_out = False
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                break
            time.sleep(0.05)

        proc = subprocess.CompletedProcess(cmd, proc.returncode if not timed_out else 124, None, None)

        if stderr_path.exists():
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            if stderr_text:
                print(stderr_text, end="", file=sys.stderr)

        if timed_out:
            print(f"error: benchmark timed out after {args.timeout:.1f}s", file=sys.stderr)

        if json_path.exists():
            proc_stdout = json_path.read_text(encoding="utf-8", errors="replace")
            if proc_stdout:
                print(proc_stdout, end="")
            try:
                json.loads(proc_stdout)
            except Exception:
                print(f"warning: output written to {json_path} but is not valid JSON", file=sys.stderr)

        stderr_path.unlink(missing_ok=True)
        if cleanup_json and json_path.exists():
            json_path.unlink(missing_ok=True)
    else:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        proc_stdout = proc.stdout
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(proc.stdout, encoding="utf-8")

    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())