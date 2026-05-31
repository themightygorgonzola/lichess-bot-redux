"""
download_lichess.py — Download a Lichess monthly database for NNUE training.

Downloads a compressed PGN from https://database.lichess.org/

Usage:
    # Download January 2025 database (recommended starting point):
    python -m training.download_lichess --month 2025-01

    # Download to a specific directory:
    python -m training.download_lichess --month 2025-01 --output-dir data/

Available databases: https://database.lichess.org/
File sizes are typically 10-30GB compressed per month.
For a first NNUE training run, even a single month is plenty.
"""

import argparse
import os
import sys
import urllib.request
import time


def download_with_progress(url: str, output_path: str):
    """Download a file with progress reporting."""
    print(f"Downloading: {url}")
    print(f"Output: {output_path}")

    req = urllib.request.Request(url, headers={'User-Agent': 'LichessBotRedux-NNUE-Trainer'})

    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} — {e.reason}")
        if e.code == 404:
            print(f"Database not found. Check available months at https://database.lichess.org/")
        sys.exit(1)

    total_size = int(response.headers.get('Content-Length', 0))
    downloaded = 0
    chunk_size = 1024 * 1024  # 1MB chunks
    t0 = time.time()

    print(f"Total size: {total_size / (1024**3):.2f} GB" if total_size else "Size unknown")

    with open(output_path, 'wb') as f:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            elapsed = time.time() - t0
            speed = downloaded / elapsed if elapsed > 0 else 0
            if total_size:
                pct = downloaded / total_size * 100
                eta = (total_size - downloaded) / speed if speed > 0 else 0
                print(f"\r  {pct:5.1f}%  {downloaded/(1024**3):.2f}/{total_size/(1024**3):.2f} GB  "
                      f"{speed/(1024**2):.1f} MB/s  ETA: {eta/60:.0f} min", end='', flush=True)
            else:
                print(f"\r  {downloaded/(1024**3):.2f} GB  {speed/(1024**2):.1f} MB/s", end='', flush=True)

    elapsed = time.time() - t0
    print(f"\n  Done. {downloaded/(1024**3):.2f} GB in {elapsed/60:.1f} minutes")


def main():
    parser = argparse.ArgumentParser(description="Download Lichess database for NNUE training")
    parser.add_argument("--month", required=True,
                        help="Month to download (YYYY-MM format, e.g. 2025-01)")
    parser.add_argument("--output-dir", default="data",
                        help="Directory to save the file (default: data/)")
    parser.add_argument("--variant", default="standard",
                        choices=["standard", "antichess", "atomic", "chess960",
                                 "crazyhouse", "horde", "kingOfTheHill",
                                 "racingKings", "threeCheck"],
                        help="Game variant (default: standard)")
    args = parser.parse_args()

    # Build URL
    url = f"https://database.lichess.org/{args.variant}/lichess_db_{args.variant}_rated_{args.month}.pgn.zst"

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    filename = f"lichess_db_{args.variant}_rated_{args.month}.pgn.zst"
    output_path = os.path.join(args.output_dir, filename)

    if os.path.exists(output_path):
        size_gb = os.path.getsize(output_path) / (1024**3)
        print(f"File already exists: {output_path} ({size_gb:.2f} GB)")
        print("Delete it to re-download.")
        sys.exit(0)

    download_with_progress(url, output_path)

    print(f"\nNext step:")
    print(f"  python -m training.label_data --pgn {output_path} --output training_data.csv --count 5000000 --workers 4 --sf-depth 10")


if __name__ == "__main__":
    main()
