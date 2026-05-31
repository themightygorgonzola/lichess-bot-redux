"""
game_compare.py  —  Stage 4 of the analysis pipeline

Compare two *_diagnosed.json files (baseline vs candidate run) and report
the SF-agreement improvement for each position category.

Usage:
    python tools/game_compare.py \\
        --baseline  results/analysis/2026-03_diagnosed.json \\
        --candidate results/analysis/2026-03_candidates_diagnosed.json

Pair matching is by (game_id, ply) so both files must cover the same positions.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(path: str) -> list[dict]:
    """Load a diagnosed JSON and return only anchor rows (no context)."""
    with open(path) as f:
        data = json.load(f)
    rows = data.get("results", [])
    anchors = [r for r in rows if r.get("window_role", "anchor") == "anchor" and "error" not in r]
    return anchors


def _key(row: dict) -> tuple:
    return (row["game_id"], row["ply"])


def _agrees(row: dict) -> bool:
    return bool(row.get("agrees_with_sf"))


def _depth_bucket(row: dict) -> str:
    d = row.get("first_sf_depth")
    if d is None:
        return "never"
    if d <= 10:
        return "d1-10"
    if d <= 15:
        return "d11-15"
    if d <= 20:
        return "d16-20"
    if d <= 25:
        return "d21-25"
    return "d26+"


def _categories(row: dict) -> list[str]:
    return row.get("categories") or []


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def compare(baseline: list[dict], candidate: list[dict]) -> dict:
    base_map = {_key(r): r for r in baseline}
    cand_map = {_key(r): r for r in candidate}

    common = set(base_map) & set(cand_map)
    if not common:
        sys.exit("[compare] No matching positions found between the two files.")

    paired = [(base_map[k], cand_map[k]) for k in sorted(common)]

    # For candidate rows that have no SF data (run with --no-sf), derive agreement
    # by comparing the candidate bot bestmove against the BASELINE SF bestmove.
    def _cand_agrees(b: dict, c: dict) -> bool:
        sf_move = b.get("sf_bestmove")
        if not sf_move:
            return False
        # If candidate ran with --no-sf, agrees_with_sf is absent; compute it.
        if c.get("sf_bestmove"):
            return bool(c.get("agrees_with_sf"))
        cand_best = c.get("bot_bestmove")
        return cand_best == sf_move

    # Overall
    base_agree = sum(_agrees(b) for b, _ in paired)
    cand_agree = sum(_cand_agrees(b, c) for b, c in paired)

    # Per category
    cat_base: dict[str, list[bool]] = defaultdict(list)
    cat_cand: dict[str, list[bool]] = defaultdict(list)
    for b, c in paired:
        cats = set(_categories(b)) | set(_categories(c))
        if not cats:
            cats = {"(uncategorised)"}
        for cat in cats:
            cat_base[cat].append(_agrees(b))
            cat_cand[cat].append(_cand_agrees(b, c))

    # Depth buckets
    depth_base: dict[str, int] = defaultdict(int)
    depth_cand: dict[str, int] = defaultdict(int)
    for b, c in paired:
        if not _agrees(b):  # only look at failures
            depth_base[_depth_bucket(b)] += 1
        if not _cand_agrees(b, c):
            depth_cand[_depth_bucket(c)] += 1

    # Per-position gain/loss
    gained = [(b, c) for b, c in paired if not _agrees(b) and _cand_agrees(b, c)]
    lost   = [(b, c) for b, c in paired if _agrees(b) and not _cand_agrees(b, c)]

    return dict(
        n         = len(paired),
        base_agree= base_agree,
        cand_agree= cand_agree,
        cat_base  = dict(cat_base),
        cat_cand  = dict(cat_cand),
        depth_base= dict(depth_base),
        depth_cand= dict(depth_cand),
        gained    = gained,
        lost      = lost,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

BAR = 20

def _bar(n: int, total: int, width: int = BAR) -> str:
    filled = round(n / total * width) if total else 0
    return "█" * filled + "░" * (width - filled)


def _pct(n: int, d: int) -> str:
    return f"{n/d*100:5.1f}%" if d else "   n/a"


def _delta_str(base: int, cand: int, denom: int) -> str:
    delta = cand - base
    pct   = delta / denom * 100 if denom else 0
    sign  = "+" if delta >= 0 else ""
    return f"{sign}{delta:+d}  ({sign}{pct:+.1f}pp)"


def print_report(result: dict, baseline_path: str, candidate_path: str):
    n         = result["n"]
    ba        = result["base_agree"]
    ca        = result["cand_agree"]

    print("╔══════════════════════════════════════════════════════════════")
    print(f"║  Stage 4 — Parameter Validation Comparison")
    print(f"║  Baseline  : {Path(baseline_path).name}")
    print(f"║  Candidate : {Path(candidate_path).name}")
    print(f"║  Positions : {n} matched pairs  (anchor only)")
    print("╚══════════════════════════════════════════════════════════════\n")

    # Overall
    print("── Overall SF Agreement ───────────────────────────────────────")
    print(f"  Baseline   {ba:3d}/{n}  {_pct(ba, n)}  {_bar(ba, n)}")
    print(f"  Candidate  {ca:3d}/{n}  {_pct(ca, n)}  {_bar(ca, n)}")
    delta_pp = (ca - ba) / n * 100 if n else 0
    sign = "▲" if delta_pp >= 0 else "▼"
    print(f"\n  Net delta  {sign} {abs(delta_pp):.1f} percentage-points  "
          f"({'improvement' if delta_pp >= 0 else 'regression'})\n")

    # Per category
    cats = sorted(
        set(result["cat_base"]) | set(result["cat_cand"]),
        key=lambda c: -(result["cat_cand"].get(c, []).count(True) - result["cat_base"].get(c, []).count(True))
    )
    print("── By Category ────────────────────────────────────────────────")
    hdr = f"  {'Category':<30}  {'Base agree':>12}  {'Cand agree':>12}  {'Delta':>14}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for cat in cats:
        bv = result["cat_base"].get(cat, [])
        cv = result["cat_cand"].get(cat, [])
        tot = max(len(bv), len(cv))
        b_ok = sum(bv)
        c_ok = sum(cv)
        delta = c_ok - b_ok
        sign = ("▲" if delta > 0 else "▼" if delta < 0 else " ")
        pct_d = delta / tot * 100 if tot else 0
        print(f"  {cat:<30}  {b_ok:3d}/{tot}  {_pct(b_ok, tot)}  "
              f"  {c_ok:3d}/{tot}  {_pct(c_ok, tot)}  "
              f"  {sign}{abs(delta):2d}  ({pct_d:+.1f}pp)")
    print()

    # Depth distribution (failures only)
    BUCKETS = ["never", "d1-10", "d11-15", "d16-20", "d21-25", "d26+"]
    b_fail = n - ba
    c_fail = n - ca
    print("── Depth-of-Agreement  (failures only) ───────────────────────")
    print(f"  {'Bucket':<10}  {'Baseline':>10}  {'Candidate':>10}  {'Delta':>8}")
    print("  " + "-" * 44)
    for bucket in BUCKETS:
        bv = result["depth_base"].get(bucket, 0)
        cv = result["depth_cand"].get(bucket, 0)
        delta = cv - bv
        sign = ("▲" if delta > 0 else "▼" if delta < 0 else " ")
        # For "never": decrease is good
        note = ""
        if bucket == "never":
            note = " ← fewer is better"
        print(f"  {bucket:<10}  {bv:6d} ({_pct(bv, b_fail)})  "
              f"{cv:6d} ({_pct(cv, c_fail)})  "
              f"  {sign}{abs(delta):2d}{note}")
    print()

    # Gained / lost positions
    gained = result["gained"]
    lost   = result["lost"]
    print(f"── Positions fixed: {len(gained)}  /  Regressions: {len(lost)} ────────────")
    if gained:
        print("  Fixed (base failed, cand agrees):")
        for b, c in gained[:10]:
            cats_str = ",".join(b.get("categories") or [])[:30]
            print(f"    {b['game_id']}  ply={b['ply']:3d}  "
                  f"base={b.get('bot_bestmove','?')} cand={c.get('bot_bestmove','?')} "
                  f"SF={b.get('sf_bestmove','?')}  [{cats_str}]")
        if len(gained) > 10:
            print(f"    … and {len(gained)-10} more")
    if lost:
        print("  Regressions (base agreed, cand fails):")
        for b, c in lost[:5]:
            cats_str = ",".join(b.get("categories") or [])[:30]
            print(f"    {b['game_id']}  ply={b['ply']:3d}  "
                  f"base={b.get('bot_bestmove','?')} cand={c.get('bot_bestmove','?')} "
                  f"SF={b.get('sf_bestmove','?')}  [{cats_str}]")
        if len(lost) > 5:
            print(f"    … and {len(lost)-5} more")
    print()

    # Verdict
    print("── Verdict ────────────────────────────────────────────────────")
    if delta_pp > 5:
        print("  ✓  Strong improvement (>5pp). Good seed for genetic_tune.py.")
    elif delta_pp > 2:
        print("  ✓  Moderate improvement (2-5pp). Worth including in GA seed.")
    elif delta_pp > 0:
        print("  ~  Marginal improvement (<2pp). Candidates are directionally correct")
        print("     but the signal is weak. GA may still find better combinations.")
    elif delta_pp == 0:
        print("  =  No change. Candidates did not affect SF agreement.")
    else:
        print("  ✗  Regression. Suggested values may overcorrect. Review candidates")
        print("     before seeding the GA.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Compare baseline vs candidate diagnosed.json files (Stage 4)."
    )
    p.add_argument("--baseline",  required=True, metavar="JSON",
                   help="Path to the original *_diagnosed.json (default params)")
    p.add_argument("--candidate", required=True, metavar="JSON",
                   help="Path to the candidate-param *_diagnosed.json")
    args = p.parse_args()

    baseline  = _load(args.baseline)
    candidate = _load(args.candidate)

    print(f"[compare] Baseline  : {len(baseline)} anchor positions")
    print(f"[compare] Candidate : {len(candidate)} anchor positions\n")

    result = compare(baseline, candidate)
    print_report(result, args.baseline, args.candidate)


if __name__ == "__main__":
    main()
