#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from engine_config import find_engine, find_nnue

PHASE0_SUITE = ROOT / "data" / "benchmarks" / "search" / "phase0_onepos.txt"
CCD_MASKS = ["0xFFFF", "0xFFFF0000"]


def run_bench(engine: str, eval_file: str | None, *, mask: str, threads: int,
              depth: int, timeout: float) -> dict:
    with tempfile.NamedTemporaryFile(prefix="mt_regression_", suffix=".json", delete=False) as tmp:
        json_path = Path(tmp.name)

    cmd = [
        engine,
        "--affinity-mask", mask,
        "--bench-search",
        "--threads", str(threads),
        "--depth", str(depth),
        "--hash", "64",
        "--warmup-depth", "0",
        "--mode", "lazysmp",
        "--json",
        "--json-file", str(json_path),
        "--suite-file", str(PHASE0_SUITE),
    ]
    if eval_file:
        cmd += ["--eval", eval_file]

    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"timeout mask={mask} threads={threads} depth={depth} after {timeout:.1f}s") from exc

    try:
        if proc.returncode != 0:
            raise AssertionError(
                f"nonzero exit mask={mask} threads={threads} depth={depth}: {proc.returncode}\n{proc.stderr}"
            )
        return json.loads(json_path.read_text(encoding="utf-8"))
    finally:
        json_path.unlink(missing_ok=True)


def first_case(summary: dict) -> dict:
    return summary["cases"][0]


def check_thread_matrix(engine: str, eval_file: str | None, timeout: float) -> None:
    print("[1/6] thread-count return matrix")
    for mask in CCD_MASKS:
        for threads in range(1, 17):
            run_bench(engine, eval_file, mask=mask, threads=threads, depth=4, timeout=timeout)
            print(f"  PASS mask={mask} threads={threads}")


def check_regression_case(engine: str, eval_file: str | None) -> None:
    print("[2/6] 8T depth-4 regression case")
    data = run_bench(engine, eval_file, mask="0xFFFF0000", threads=8, depth=4, timeout=5.0)
    c = first_case(data)
    assert c["bestmove"] == "a1c1", c
    assert c["elapsed_ms"] <= 1000, c
    assert c["nodes"] > 0, c
    print(f"  PASS mask=0xFFFF0000 threads=8 elapsed_ms={c['elapsed_ms']} bestmove={c['bestmove']}")


def check_single_thread_sanity(engine: str, eval_file: str | None) -> None:
    print("[3/6] 1T strength sanity")
    data = run_bench(engine, eval_file, mask="0xFFFF", threads=1, depth=4, timeout=5.0)
    c = first_case(data)
    assert c["bestmove"] == "a2a3", c
    assert c["ponder"] == "b5b4", c
    assert c["seldepth"] >= 8, c
    assert c["nodes"] >= 1000, c
    print(f"  PASS mask=0xFFFF threads=1 bestmove={c['bestmove']} seldepth={c['seldepth']}")


def check_mt_timing_sanity(engine: str, eval_file: str | None) -> None:
    print("[4/6] MT benchmark return-time sanity")
    for mask, threads in [("0xFFFF", 8), ("0xFFFF", 16), ("0xFFFF0000", 8), ("0xFFFF0000", 16)]:
        data = run_bench(engine, eval_file, mask=mask, threads=threads, depth=4, timeout=5.0)
        c = first_case(data)
        assert c["bestmove"] == "a1c1", c
        assert c["elapsed_ms"] <= 1000, c
        print(f"  PASS mask={mask} threads={threads} elapsed_ms={c['elapsed_ms']}")


def check_deeper_mt_sanity(engine: str, eval_file: str | None, timeout: float) -> None:
    print("[5/6] deeper MT sanity")
    for mask, threads, depth, max_elapsed in [
        ("0xFFFF0000", 8, 6, 2500),
        ("0xFFFF", 16, 6, 2500),
    ]:
        data = run_bench(engine, eval_file, mask=mask, threads=threads, depth=depth, timeout=timeout)
        c = first_case(data)
        assert c["elapsed_ms"] <= max_elapsed, c
        assert c["nodes"] >= 50000, c
        assert c["seldepth"] >= 5, c
        smp = c["smp"]
        assert smp["root_jobs_completed"] == smp["root_job_count"], c
        assert smp["splits_created"] >= 1, c
        assert smp["splits_completed"] >= 1, c
        print(
            f"  PASS mask={mask} threads={threads} depth={depth} "
            f"elapsed_ms={c['elapsed_ms']} bestmove={c['bestmove']} nodes={c['nodes']} "
            f"splits={smp['splits_created']} helper_jobs={smp['helper_split_jobs']}"
        )


def check_repeated_mt_stability(engine: str, eval_file: str | None, repeats: int, timeout: float) -> None:
    print("[6/6] repeated MT stability")
    for mask, threads, depth, max_elapsed in [
        ("0xFFFF0000", 8, 4, 1000),
        ("0xFFFF", 16, 4, 1000),
        ("0xFFFF0000", 8, 6, 2500),
        ("0xFFFF", 16, 6, 2500),
    ]:
        bestmove = None
        times: list[int] = []
        for _ in range(repeats):
            data = run_bench(engine, eval_file, mask=mask, threads=threads, depth=depth, timeout=timeout)
            c = first_case(data)
            assert c["elapsed_ms"] <= max_elapsed, c
            if bestmove is None:
                bestmove = c["bestmove"]
            assert c["bestmove"] == bestmove, c
            if depth >= 6:
                smp = c["smp"]
                assert smp["splits_created"] >= 1, c
            times.append(c["elapsed_ms"])
        print(
            f"  PASS mask={mask} threads={threads} depth={depth} "
            f"bestmove={bestmove} times={times}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify current MT stability fallback regressions")
    ap.add_argument("--engine", default="", help="Optional engine path")
    ap.add_argument("--eval-file", default="", help="Optional eval file path")
    ap.add_argument("--matrix-timeout", type=float, default=5.0,
                    help="Timeout per matrix case in seconds")
    ap.add_argument("--deep-timeout", type=float, default=10.0,
                    help="Timeout per deeper/repeated case in seconds")
    ap.add_argument("--repeat-runs", type=int, default=3,
                    help="Number of repeated stability runs for selected MT cases")
    args = ap.parse_args()

    engine = find_engine(args.engine or None)
    eval_file = find_nnue(args.eval_file or None)

    check_thread_matrix(engine, eval_file, args.matrix_timeout)
    check_regression_case(engine, eval_file)
    check_single_thread_sanity(engine, eval_file)
    check_mt_timing_sanity(engine, eval_file)
    check_deeper_mt_sanity(engine, eval_file, args.deep_timeout)
    check_repeated_mt_stability(engine, eval_file, args.repeat_runs, args.deep_timeout)
    print("ALL MT REGRESSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
