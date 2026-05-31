#!/usr/bin/env python3
"""
nps_hw_sweep.py â€” Hardware NPS scaling sweep: redux-hce vs Stockfish.

Runs both engines at a configurable list of thread counts on the same
position suite and prints a side-by-side comparison table with
hardware context, scaling efficiency, and per-node cost ratios.

Usage
-----
  python tools/nps_hw_sweep.py                        # default: d14, threads 1 2 4 8 16 32
  python tools/nps_hw_sweep.py --threads 1 4 8 16 32  # custom thread list
  python tools/nps_hw_sweep.py --depth 12             # shallower/faster
  python tools/nps_hw_sweep.py --no-sf                # redux only (no SF comparison)
  python tools/nps_hw_sweep.py --json out.json        # save results as JSON
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# â”€â”€ Paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

ROOT = Path(__file__).resolve().parent.parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from engine_config import PROJECT_ROOT

REDUX_HCE = ROOT / "bot" / "engine" / "redux-hce.exe"
SF_EXE    = ROOT / "engines" / "stockfish-17.1" / "stockfish" / "stockfish-windows-x86-64-avx2.exe"
SUITE     = ROOT / "data" / "benchmarks" / "search" / "nps_core.txt"

# â”€â”€ ANSI helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _enable_ansi() -> bool:
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False

_color = os.name == "nt" and _enable_ansi()

def _c(code: str, t: str) -> str:
    try:
        if not sys.stdout.isatty():
            return t
    except Exception:
        return t
    return f"\033[{code}m{t}\033[0m"

def green(t):  return _c("92", t)
def yellow(t): return _c("93", t)
def cyan(t):   return _c("96", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)

# â”€â”€ Hardware info â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class HardwareInfo:
    cpu_name: str = ""
    physical_cores: int = 0
    logical_cores: int = 0
    max_clock_mhz: int = 0
    ram_gb: float = 0.0
    os_name: str = ""

def get_hardware_info() -> HardwareInfo:
    info = HardwareInfo(os_name=platform.system() + " " + platform.version()[:20])
    if os.name == "nt":
        try:
            import subprocess as sp
            # PowerShell is more reliable than wmic CSV on all Win11 versions
            ps_cmd = (
                "Get-WmiObject Win32_Processor | "
                "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed | "
                "ConvertTo-Csv -NoTypeInformation"
            )
            out = sp.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                text=True, timeout=10, creationflags=getattr(sp, "CREATE_NO_WINDOW", 0)
            )
            lines = [l.strip().strip('"') for l in out.strip().splitlines()]
            if len(lines) >= 2:
                # header: Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed
                vals = [v.strip('"') for v in lines[1].split(",")]
                if len(vals) >= 4:
                    info.cpu_name       = vals[0]
                    info.physical_cores = int(vals[1])
                    info.logical_cores  = int(vals[2])
                    info.max_clock_mhz  = int(vals[3])
            ram_cmd = (
                "Get-WmiObject Win32_ComputerSystem | "
                "Select-Object TotalPhysicalMemory | "
                "ConvertTo-Csv -NoTypeInformation"
            )
            ram_out = sp.check_output(
                ["powershell", "-NoProfile", "-Command", ram_cmd],
                text=True, timeout=10, creationflags=getattr(sp, "CREATE_NO_WINDOW", 0)
            )
            ram_lines = [l.strip() for l in ram_out.strip().splitlines()]
            if len(ram_lines) >= 2:
                val = ram_lines[1].strip('"')
                info.ram_gb = round(int(val) / 1024**3, 1)
        except Exception:
            pass
    return info

# â”€â”€ Engine runners â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@dataclass
class BenchResult:
    threads: int = 0
    total_nps: int = 0
    median_nps: int = 0
    total_nodes: int = 0
    elapsed_ms: int = 0
    raw_output: str = ""
    error: str = ""

def run_redux(threads: int, depth: int, hash_mb: int, suite_file: Path) -> BenchResult:
    """Run redux-hce --bench-search and parse output."""
    cmd = [
        str(REDUX_HCE),
        "--bench-search",
        "--threads", str(threads),
        "--depth", str(depth),
        "--hash", str(hash_mb),
        "--suite-file", str(suite_file),
    ]
    r = BenchResult(threads=threads)
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT),
            capture_output=True, text=True, timeout=600,
        )
        r.raw_output = proc.stdout + proc.stderr
        for line in r.raw_output.splitlines():
            line = line.strip()
            if line.startswith("Total NPS:"):
                try:
                    r.total_nps = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line.startswith("Median NPS:"):
                try:
                    r.median_nps = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line.startswith("Total nodes:"):
                try:
                    r.total_nodes = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line.startswith("Total time:"):
                try:
                    # "Total time:   1234 ms (1234567 us)"
                    r.elapsed_ms = int(line.split("ms")[0].split(":")[-1].strip())
                except ValueError:
                    pass
    except subprocess.TimeoutExpired:
        r.error = "TIMEOUT"
    except Exception as e:
        r.error = str(e)
    return r


def run_sf(threads: int, depth: int, hash_mb: int) -> BenchResult:
    """Run SF bench <hash> <threads> <depth> and parse output."""
    r = BenchResult(threads=threads)
    if not SF_EXE.is_file():
        r.error = "SF not found"
        return r
    try:
        # SF bench command: bench <hash> <threads> <depth>
        stdin_cmds = f"bench {hash_mb} {threads} {depth}\nquit\n"
        proc = subprocess.run(
            [str(SF_EXE)],
            input=stdin_cmds,
            cwd=str(ROOT),
            capture_output=True, text=True, timeout=600,
        )
        r.raw_output = proc.stdout + proc.stderr
        for line in r.raw_output.splitlines():
            line = line.strip()
            if line.startswith("Nodes/second"):
                try:
                    r.total_nps = int(line.split(":")[-1].strip())
                    r.median_nps = r.total_nps  # SF only reports aggregate
                except ValueError:
                    pass
            elif line.startswith("Nodes searched"):
                try:
                    r.total_nodes = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line.startswith("Total time"):
                try:
                    r.elapsed_ms = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
    except subprocess.TimeoutExpired:
        r.error = "TIMEOUT"
    except Exception as e:
        r.error = str(e)
    return r

# â”€â”€ Formatting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def fmt_nps(n: int) -> str:
    if n == 0:
        return "â€”"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)

def fmt_ratio(a: int, b: int) -> str:
    if b == 0 or a == 0:
        return "â€”"
    return f"{a/b:.2f}Ã—"

# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def main() -> int:
    ap = argparse.ArgumentParser(description="Hardware NPS sweep: redux-hce vs Stockfish")
    ap.add_argument("--threads", nargs="+", type=int,
                    default=[1, 2, 4, 8, 12, 16, 24, 32],
                    help="Thread counts to test (default: 1 2 4 8 12 16 24 32)")
    ap.add_argument("--depth", type=int, default=14,
                    help="Search depth (default: 14)")
    ap.add_argument("--hash-per-thread", type=int, default=32,
                    help="Hash MB per thread for redux (default: 32, capped at 2048 total)")
    ap.add_argument("--sf-hash-per-thread", type=int, default=64,
                    help="Hash MB per thread for SF (default: 64)")
    ap.add_argument("--no-sf", action="store_true",
                    help="Skip Stockfish comparison")
    ap.add_argument("--json", default="", metavar="FILE",
                    help="Save full results as JSON")
    ap.add_argument("--suite-file", default="", metavar="FILE",
                    help="Custom suite file (default: data/benchmarks/search/nps_core.txt)")
    args = ap.parse_args()

    suite_file = Path(args.suite_file) if args.suite_file else SUITE
    if not suite_file.is_file():
        print(f"ERROR: suite file not found: {suite_file}", file=sys.stderr)
        return 1

    if not REDUX_HCE.is_file():
        print(f"ERROR: redux-hce not found: {REDUX_HCE}", file=sys.stderr)
        return 1

    run_sf_flag = not args.no_sf and SF_EXE.is_file()

    # â”€â”€ Header â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    hw = get_hardware_info()
    print()
    print(bold("Hardware NPS Sweep"))
    print("=" * 78)
    if hw.cpu_name:
        print(f"  CPU  : {hw.cpu_name}")
        print(f"  Cores: {hw.physical_cores} physical / {hw.logical_cores} logical  "
              f"@ {hw.max_clock_mhz} MHz")
        print(f"  RAM  : {hw.ram_gb} GB")
    print(f"  Suite: {suite_file.name}  |  Depth: {args.depth}")
    print(f"  Redux: {REDUX_HCE.name}")
    if run_sf_flag:
        print(f"  SF   : {SF_EXE.name}")
    print()

    # â”€â”€ Column headers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if run_sf_flag:
        hdr = (f"{'Threads':>8}  "
               f"{'Redux Total':>14}  {'Redux Median':>13}  {'Redux Scale':>12}  "
               f"{'SF NPS':>14}  {'SF Scale':>9}  "
               f"{'HCE/SF':>8}  {'SF/HCE':>8}")
    else:
        hdr = (f"{'Threads':>8}  "
               f"{'Redux Total':>14}  {'Redux Median':>13}  {'Redux Scale':>12}")
    print(cyan(hdr))
    print(dim("â”€" * len(hdr)))

    # â”€â”€ Run and print â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    results = []
    baseline_hce = 0
    baseline_sf  = 0

    for t in args.threads:
        hash_hce = min(args.hash_per_thread * t, 2048)
        hash_sf  = min(args.sf_hash_per_thread * t, 2048)

        sys.stdout.write(f"  {t:>2}T  running...")
        sys.stdout.flush()

        hce = run_redux(t, args.depth, hash_hce, suite_file)
        sf  = run_sf(t, args.depth, hash_sf) if run_sf_flag else BenchResult(threads=t)

        # Baselines
        if t == args.threads[0]:
            baseline_hce = hce.total_nps or 1
            baseline_sf  = sf.total_nps  or 1

        hce_scale = fmt_ratio(hce.total_nps, baseline_hce)
        sf_scale  = fmt_ratio(sf.total_nps,  baseline_sf)
        hce_sf    = fmt_ratio(hce.total_nps, sf.total_nps)
        sf_hce    = fmt_ratio(sf.total_nps,  hce.total_nps)

        sys.stdout.write("\r")  # clear "running..." line

        err_tag = ""
        if hce.error:
            err_tag = f"  [HCE ERROR: {hce.error}]"
        if sf.error and run_sf_flag:
            err_tag += f"  [SF ERROR: {sf.error}]"

        if run_sf_flag:
            row = (f"  {t:>8}  "
                   f"{fmt_nps(hce.total_nps):>14}  {fmt_nps(hce.median_nps):>13}  {hce_scale:>12}  "
                   f"{fmt_nps(sf.total_nps):>14}  {sf_scale:>9}  "
                   f"{hce_sf:>8}  {sf_hce:>8}"
                   f"{err_tag}")
        else:
            row = (f"  {t:>8}  "
                   f"{fmt_nps(hce.total_nps):>14}  {fmt_nps(hce.median_nps):>13}  {hce_scale:>12}"
                   f"{err_tag}")

        print(row)

        results.append({
            "threads": t,
            "redux": asdict(hce),
            "sf": asdict(sf) if run_sf_flag else None,
        })

    # â”€â”€ Interpretation notes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print()
    print(bold("Notes"))
    print("â”€" * 50)
    print("  NPS = total nodes Ã· wall time across all positions in suite.")
    print("  'Redux Total' sums nodes across all threads (real throughput).")
    print("  'Redux Median' = median single-position NPS (less affected by outliers).")
    print("  SF reports aggregate Nodes/second from its own bench suite.")
    print("  HCE nodes are cheap (no NNUE); SF nodes cost ~5-8Ã— more compute.")
    print("  Raw NPS ratio does NOT equal strength ratio.")
    print()
    if hw.physical_cores:
        peak_t = hw.physical_cores
        print(f"  Physical core saturation at {peak_t}T. "
              f"SMT ({hw.logical_cores}T) adds ~20-30% on top at best.")
    print()

    # â”€â”€ JSON output â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "hardware": asdict(hw),
            "config": {
                "depth": args.depth,
                "suite": str(suite_file),
                "redux_binary": str(REDUX_HCE),
                "sf_binary": str(SF_EXE) if run_sf_flag else None,
                "hash_per_thread_redux": args.hash_per_thread,
                "hash_per_thread_sf": args.sf_hash_per_thread,
            },
            "results": results,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"  JSON saved â†’ {out_path}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
