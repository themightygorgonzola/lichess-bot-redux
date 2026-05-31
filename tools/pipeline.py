"""
Lichess monthly database download + conversion pipeline.

Commands:
  status [--verbose]             Show pipeline state
  add <YYYY-MM> [<YYYY-MM> ...]  Queue month(s)
  add-range <YYYY-MM> <YYYY-MM>  Queue inclusive month range
  add-all                        Queue all months found on database.lichess.org
  reset <YYYY-MM> [...]          Reset month(s) back to pending
  skip <YYYY-MM> [...]           Mark month(s) as skipped
  run [--max-positions N] [--min-elo N] [--min-time-control N]
                                 Process next pending month
  run-all [same options]         Process all pending months in order

Usage example:
  python tools/pipeline.py add-all
  python tools/pipeline.py run-all --max-positions 25000000 --min-elo 1800
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BIN_DIR = DATA_DIR / "training"
PGN_DIR = DATA_DIR / "pgn"
STATE_FILE = DATA_DIR / "pipeline_state.json"
PREP_DATA = ROOT / "tools" / "prep_data.py"

BIN_DIR.mkdir(parents=True, exist_ok=True)
PGN_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://database.lichess.org/"
SHA_URL = BASE_URL + "standard/sha256sums.txt"
RECORD_BYTES = 136
MIN_FREE_GB = 50.0  # abort if disk drops below this

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state() -> Dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Support both flat {"YYYY-MM": {...}} and wrapped {"months": {...}}
        if "months" in raw and isinstance(raw["months"], dict):
            return raw["months"]
        return raw
    return {}


def _save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"months": state}, f, indent=2)


def _month_key(ym: str) -> str:
    """Normalise YYYY-MM string."""
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        raise ValueError(f"Invalid month format: {ym!r}  (expected YYYY-MM)")
    return ym


# ---------------------------------------------------------------------------
# Disk helpers
# ---------------------------------------------------------------------------

def _free_gb(path: Path = ROOT) -> float:
    import shutil
    return shutil.disk_usage(path).free / (1024 ** 3)


# ---------------------------------------------------------------------------
# Lichess DB index
# ---------------------------------------------------------------------------

def _fetch_available_months() -> List[str]:
    """Return sorted list of YYYY-MM strings available on database.lichess.org."""
    url = BASE_URL + "standard/list.txt"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            text = r.read().decode("utf-8", errors="replace")
        months = sorted(re.findall(r"\d{4}-\d{2}", text))
        if months:
            return months
    except Exception:
        pass
    # Fallback: scrape the HTML index
    with urllib.request.urlopen(BASE_URL, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    months = sorted(set(re.findall(r"\d{4}-\d{2}", html)))
    return months


def _pgn_url(ym: str) -> str:
    return BASE_URL + f"standard/lichess_db_standard_rated_{ym}.pgn.zst"


def _fetch_sha256sums() -> Dict[str, str]:
    """Return {filename: sha256hex} from Lichess SHA sums file."""
    with urllib.request.urlopen(SHA_URL, timeout=30) as r:
        text = r.read().decode("utf-8", errors="replace")
    result: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[1].lstrip("*")] = parts[0]
    return result


# ---------------------------------------------------------------------------
# Download with resume
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path, expected_sha: Optional[str] = None) -> Path:
    """Download url to dest, resuming if partial file exists."""
    headers: Dict[str, str] = {}
    existing = dest.stat().st_size if dest.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"

    req = urllib.request.Request(url, headers={**headers, "User-Agent": "lichess-pipeline/1"})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and existing:
            # 416 Range Not Satisfiable: server says file is already complete
            print(f"  File already complete ({existing/(1<<20):.1f} MB), skipping download.")
            if expected_sha:
                print("  Verifying SHA256 ...", end=" ", flush=True)
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for buf in iter(lambda: f.read(1 << 20), b""):
                        h.update(buf)
                got = h.hexdigest()
                if got != expected_sha:
                    dest.unlink(missing_ok=True)
                    raise RuntimeError(f"SHA256 mismatch: expected {expected_sha} got {got}")
                print("OK")
            return dest
        raise RuntimeError(f"Download error: {e}")
    except Exception as e:
        raise RuntimeError(f"Download error: {e}")

    total_header = resp.headers.get("Content-Length") or resp.headers.get("x-content-length")
    total = int(total_header) + existing if total_header else None
    mode = "ab" if existing and resp.status == 206 else "wb"
    if mode == "wb":
        existing = 0

    chunk = 1 << 20  # 1 MB
    written = existing
    t0 = time.time()
    with open(dest, mode) as f:
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            f.write(buf)
            written += len(buf)
            if total:
                pct = written / total * 100
                mb = written / (1 << 20)
                elapsed = time.time() - t0 + 1e-6
                speed = (written - existing) / elapsed / (1 << 20)
                print(f"\r  {pct:5.1f}%  {mb:7.0f} MB  {speed:5.1f} MB/s", end="", flush=True)

    print()  # newline after progress

    if expected_sha:
        print("  Verifying SHA256 ...", end=" ", flush=True)
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for buf in iter(lambda: f.read(1 << 20), b""):
                h.update(buf)
        got = h.hexdigest()
        if got != expected_sha:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 mismatch: expected {expected_sha} got {got}")
        print("OK")

    return dest


# ---------------------------------------------------------------------------
# Bin verification
# ---------------------------------------------------------------------------

def _verify_bin(path: Path) -> int:
    """Return record count. Files have a 32-byte header (NNUE_BIN magic)."""
    try:
        from ml.data import HEADER_SIZE, HEADER_MAGIC
    except ModuleNotFoundError:
        # ml package not on sys.path — resolve it relative to this file's repo root
        import sys as _sys
        _repo_root = str(Path(__file__).resolve().parent.parent)
        if _repo_root not in _sys.path:
            _sys.path.insert(0, _repo_root)
        from ml.data import HEADER_SIZE, HEADER_MAGIC
    import struct
    size = path.stat().st_size
    if size < HEADER_SIZE:
        raise RuntimeError(f"{path.name}: too small ({size} bytes), missing header")
    with open(path, 'rb') as f:
        raw = f.read(HEADER_SIZE)
    magic = raw[:8]
    if magic != HEADER_MAGIC:
        raise RuntimeError(f"{path.name}: bad magic {magic!r}, expected {HEADER_MAGIC!r}")
    data_bytes = size - HEADER_SIZE
    if data_bytes % RECORD_BYTES != 0:
        raise RuntimeError(
            f"{path.name}: data section {data_bytes} is not a multiple of {RECORD_BYTES}"
        )
    return data_bytes // RECORD_BYTES


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_month(
    ym: str,
    state: Dict,
    *,
    max_positions: Optional[int] = None,
    min_elo: int = 1500,
    min_time_control: int = 60,
    result_wdl_blend: float = 0.0,
    sample_every: int = 3,
    skip_plies: int = 10,
    score_cap: int = 3000,
) -> None:
    key = _month_key(ym)
    entry = state.get(key, {})

    pgn_name = f"lichess_db_standard_rated_{ym}.pgn.zst"
    pgn_path = PGN_DIR / pgn_name
    bin_path = BIN_DIR / f"q{ym.replace('-', '_')}.bin"

    # -- already done? --
    if entry.get("state") == "done":
        print(f"[{ym}] already done ({entry.get('records',0):,} records) -- skip")
        return

    print(f"\n{'='*60}")
    print(f"  Processing {ym}")
    print(f"{'='*60}")

    # -- disk check --
    free = _free_gb()
    print(f"  Disk free: {free:.1f} GB")
    if free < MIN_FREE_GB:
        raise RuntimeError(f"Only {free:.1f} GB free -- stopping (need >= {MIN_FREE_GB} GB)")

    # -- get SHA --
    print("  Fetching SHA256 sums ...")
    try:
        shas = _fetch_sha256sums()
        expected_sha = shas.get(pgn_name)
    except Exception as e:
        print(f"  WARNING: could not fetch SHA sums: {e}")
        expected_sha = None

    # -- download --
    url = _pgn_url(ym)
    if not pgn_path.exists() or pgn_path.stat().st_size == 0:
        print(f"  Downloading {url}")
        state[key] = {**entry, "state": "downloading", "pgn_path": str(pgn_path)}
        _save_state(state)
    else:
        print(f"  Resuming download: {pgn_path.name} ({pgn_path.stat().st_size/(1<<30):.2f} GB already)")

    try:
        _download(url, pgn_path, expected_sha=expected_sha)
    except Exception as e:
        state[key] = {**state.get(key, {}), "state": "error", "error": str(e)}
        _save_state(state)
        raise

    state[key] = {**state.get(key, {}), "state": "converting", "pgn_path": str(pgn_path)}
    _save_state(state)

    # -- convert --
    print(f"  Converting to {bin_path.name} ...")
    cmd = [
        sys.executable, str(PREP_DATA),
        "--pgn", str(pgn_path),
        "--output", str(bin_path),
        "--min-elo", str(min_elo),
        "--min-time-control", str(min_time_control),
        "--sample-every", str(sample_every),
        "--skip-plies", str(skip_plies),
        "--score-cap", str(score_cap),
        "--result-wdl-blend", str(result_wdl_blend),
    ]
    if max_positions:
        cmd += ["--max-positions", str(max_positions)]

    t0 = time.time()
    ret = subprocess.call(cmd)
    elapsed = time.time() - t0

    if ret != 0:
        # Retry with a single worker - avoids BrokenProcessPool on some months
        print(f"  prep_data failed (exit {ret}), retrying with --workers 1 ...")
        cmd_single = [c for c in cmd if not c.startswith('--workers')] + ['--workers', '1']
        t0 = time.time()
        ret = subprocess.call(cmd_single)
        elapsed = time.time() - t0

    if ret != 0:
        state[key] = {**state.get(key, {}), "state": "error", "error": f"prep_data exit {ret}"}
        _save_state(state)
        raise RuntimeError(f"prep_data.py failed with exit code {ret}")

    # -- verify bin --
    print("  Verifying .bin ...")
    try:
        records = _verify_bin(bin_path)
    except RuntimeError as e:
        state[key] = {**state.get(key, {}), "state": "error", "error": str(e)}
        _save_state(state)
        raise

    size_gb = bin_path.stat().st_size / (1 << 30)
    print(f"  OK: {records:,} records  {size_gb:.2f} GB  ({elapsed/60:.1f} min)")

    # -- delete pgn --
    pgn_size_gb = pgn_path.stat().st_size / (1 << 30)
    print(f"  Deleting {pgn_path.name} ({pgn_size_gb:.1f} GB) ...")
    pgn_path.unlink()
    print(f"  Freed {pgn_size_gb:.1f} GB")

    state[key] = {
        "state": "done",
        "bin_path": str(bin_path),
        "records": records,
        "bin_size_gb": round(size_gb, 2),
        "elapsed_min": round(elapsed / 60, 1),
    }
    _save_state(state)
    print(f"  [{ym}] DONE")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(state: Dict, verbose: bool = False) -> None:
    months = sorted(state.keys())
    counts: Dict[str, int] = {}
    for key in months:
        s = state[key].get("state", "pending")
        counts[s] = counts.get(s, 0) + 1

    done = counts.get("done", 0)
    pending = counts.get("pending", 0)
    downloading = counts.get("downloading", 0)
    converting = counts.get("converting", 0)
    error = counts.get("error", 0)
    skipped = counts.get("skipped", 0)
    total = len(months)

    total_records = sum(
        state[k].get("records", 0) for k in months if state[k].get("state") == "done"
    )

    print(f"\nPipeline status")
    print(f"  Total months tracked : {total}")
    print(f"  Done                 : {done}")
    print(f"  Pending              : {pending}")
    print(f"  Downloading          : {downloading}")
    print(f"  Converting           : {converting}")
    print(f"  Error                : {error}")
    print(f"  Skipped              : {skipped}")
    print(f"  Total records (done) : {total_records:,}")
    print(f"  Disk free            : {_free_gb():.1f} GB")

    if verbose:
        print()
        for key in months:
            s = state[key].get("state", "pending")
            extra = ""
            if s == "done":
                extra = f"  {state[key].get('records',0):>12,} rec  {state[key].get('bin_size_gb',0):.2f} GB"
            elif s == "error":
                extra = f"  ERR: {state[key].get('error','')[:60]}"
            print(f"  {key}  [{s:<12}]{extra}")


def cmd_add(state: Dict, months: List[str]) -> None:
    added = 0
    for ym in months:
        key = _month_key(ym)
        if key not in state:
            state[key] = {"state": "pending"}
            added += 1
        else:
            print(f"  {key}: already tracked (state={state[key].get('state')})")
    _save_state(state)
    print(f"Added {added} month(s).")


def cmd_add_range(state: Dict, start: str, end: str) -> None:
    start_key = _month_key(start)
    end_key = _month_key(end)
    months: List[str] = []
    y, m = int(start_key[:4]), int(start_key[5:])
    ey, em = int(end_key[:4]), int(end_key[5:])
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    cmd_add(state, months)


def cmd_add_all(state: Dict) -> None:
    print("Fetching month list from database.lichess.org ...")
    months = _fetch_available_months()
    print(f"Found {len(months)} months: {months[0]} to {months[-1]}")
    cmd_add(state, months)


def cmd_reset(state: Dict, months: List[str]) -> None:
    for ym in months:
        key = _month_key(ym)
        state[key] = {"state": "pending"}
        print(f"  Reset {key}")
    _save_state(state)


def cmd_skip(state: Dict, months: List[str]) -> None:
    for ym in months:
        key = _month_key(ym)
        state[key] = {**state.get(key, {}), "state": "skipped"}
        print(f"  Skipped {key}")
    _save_state(state)


def cmd_run(state: Dict, args: argparse.Namespace) -> None:
    pending = sorted(
        k for k, v in state.items() if v.get("state") == "pending"
    )
    if not pending:
        print("No pending months.")
        return
    ym = pending[0]
    print(f"Running next pending month: {ym}")
    process_month(
        ym, state,
        max_positions=getattr(args, "max_positions", None),
        min_elo=getattr(args, "min_elo", 1500),
        min_time_control=getattr(args, "min_time_control", 60),
        result_wdl_blend=getattr(args, "result_wdl_blend", 0.0),
        sample_every=getattr(args, "sample_every", 3),
        skip_plies=getattr(args, "skip_plies", 10),
        score_cap=getattr(args, "score_cap", 3000),
    )


def cmd_run_all(state: Dict, args: argparse.Namespace) -> None:
    pending = sorted(
        k for k, v in state.items() if v.get("state") == "pending"
    )
    print(f"{len(pending)} pending months to process.")
    for i, ym in enumerate(pending, 1):
        print(f"\n[{i}/{len(pending)}] Starting {ym} ...")
        try:
            process_month(
                ym, state,
                max_positions=getattr(args, "max_positions", None),
                min_elo=getattr(args, "min_elo", 1500),
                min_time_control=getattr(args, "min_time_control", 60),
                result_wdl_blend=getattr(args, "result_wdl_blend", 0.0),
                sample_every=getattr(args, "sample_every", 3),
                skip_plies=getattr(args, "skip_plies", 10),
                score_cap=getattr(args, "score_cap", 3000),
            )
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            print(f"  ERROR processing {ym}: {e}")
            print("  Continuing to next month ...")
            continue
    print("\nrun-all complete.")
    cmd_status(state)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Lichess database download and conversion pipeline"
    )
    sub = parser.add_subparsers(dest="command")

    # status
    p_status = sub.add_parser("status")
    p_status.add_argument("--verbose", "-v", action="store_true")

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("months", nargs="+")

    # add-range
    p_range = sub.add_parser("add-range")
    p_range.add_argument("start")
    p_range.add_argument("end")

    # add-all
    sub.add_parser("add-all")

    # reset
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("months", nargs="+")

    # skip
    p_skip = sub.add_parser("skip")
    p_skip.add_argument("months", nargs="+")

    def _add_run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-positions", type=int, default=None, dest="max_positions")
        p.add_argument("--min-elo", type=int, default=1500, dest="min_elo")
        p.add_argument("--min-time-control", type=int, default=60, dest="min_time_control")
        p.add_argument("--result-wdl-blend", type=float, default=0.0, dest="result_wdl_blend",
                       help="Blend actual game result into stored WDL (0.25 recommended for human PGN)")
        p.add_argument("--sample-every", type=int, default=3, dest="sample_every",
                       help="Keep 1 in N positions per game (6 reduces correlation)")
        p.add_argument("--skip-plies", type=int, default=10, dest="skip_plies",
                       help="Skip first N plies of each game")
        p.add_argument("--score-cap", type=int, default=3000, dest="score_cap",
                       help="Cap |score| at this value before WDL derivation")

    # run
    p_run = sub.add_parser("run")
    _add_run_args(p_run)

    # run-all
    p_run_all = sub.add_parser("run-all")
    _add_run_args(p_run_all)

    args = parser.parse_args(argv)
    state = _load_state()

    if args.command == "status":
        cmd_status(state, verbose=args.verbose)
    elif args.command == "add":
        cmd_add(state, args.months)
    elif args.command == "add-range":
        cmd_add_range(state, args.start, args.end)
    elif args.command == "add-all":
        cmd_add_all(state)
    elif args.command == "reset":
        cmd_reset(state, args.months)
    elif args.command == "skip":
        cmd_skip(state, args.months)
    elif args.command == "run":
        cmd_run(state, args)
    elif args.command == "run-all":
        cmd_run_all(state, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
