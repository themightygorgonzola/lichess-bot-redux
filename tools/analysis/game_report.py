#!/usr/bin/env python3
"""
game_report.py â€” Stage 3 of the game analysis pipeline.

Reads the output of game_diagnose.py, aggregates diagnostic findings across
all positions, and produces:

  1. A human-readable console/markdown report:
       - Overall accuracy vs Stockfish
       - Breakdown by phase (opening / middlegame / endgame)
       - Breakdown by position category (passed_pawns, king_safety, pins, ...)
       - Depth analysis: at what depth do we first agree with SF?
       - Biggest recurring failures

  2. param_candidates.json â€” a ranked list of parameter adjustments for
     genetic_tune.py, derived from which categories show the most failures.
     Each entry includes: parameter name, current default, suggested direction,
     estimated magnitude, and the evidence categories that motivated it.

Usage:
    python tools/game_report.py --input results/analysis/2026-03_diagnosed.json

    # Write markdown to file
    python tools/game_report.py --input ... --md results/analysis/report.md

    # Only print summary, skip param candidates
    python tools/game_report.py --input ... --no-params

Output:
    Console report
    results/analysis/<tag>_report.md        (if --md given)
    results/analysis/<tag>_param_candidates.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.dirname(os.path.dirname(_TOOLS_DIR))
_OUT_DIR   = os.path.join(_ROOT, "results", "analysis")


# ---------------------------------------------------------------------------
# Tunable parameter defaults (mirror of genetic_tune.py â€” kept in sync manually)
# ---------------------------------------------------------------------------

TUNABLE_PARAMS: list[tuple[str, int, int, int, int]] = [
    ("EvalDoubledPawnPenalty",        -15, -60,    0,  3),
    ("EvalIsolatedPawnPenalty",       -20, -60,    0,  3),
    ("EvalPassedPawnBonusR1",          10,   0,   50,  3),
    ("EvalPassedPawnBonusR2",          15,   0,   60,  3),
    ("EvalPassedPawnBonusR3",          25,   0,  100,  5),
    ("EvalPassedPawnBonusR4",          45,   0,  150,  5),
    ("EvalPassedPawnBonusR5",          75,   0,  200,  8),
    ("EvalPassedPawnBonusR6",         120,   0,  300, 10),
    ("EvalPassedPawnEGR1",             15,   0,   60,  3),
    ("EvalPassedPawnEGR2",             22,   0,   80,  4),
    ("EvalPassedPawnEGR3",             38,   0,  120,  5),
    ("EvalPassedPawnEGR4",             68,   0,  200,  8),
    ("EvalPassedPawnEGR5",            112,   0,  300, 10),
    ("EvalPassedPawnEGR6",            180,   0,  500, 15),
    ("EvalProtectedPasserMG",          10,   0,   50,  3),
    ("EvalProtectedPasserEG",          20,   0,   80,  5),
    ("EvalCandidatePasserMG",           5,   0,   30,  2),
    ("EvalCandidatePasserEG",          10,   0,   50,  3),
    ("EvalBishopPairBonus",            30,   0,  100,  5),
    ("EvalBishopPairEGBonus",          45,   0,  100,  5),
    ("EvalRookOpenFileBonus",          25,   0,   60,  4),
    ("EvalRookSemiOpenBonus",          14,   0,   40,  3),
    ("EvalRookOpenFileEG",             15,   0,   40,  3),
    ("EvalRookSemiOpenEG",              8,   0,   25,  2),
    ("EvalRookSeventhMG",              30,   0,   80,  5),
    ("EvalRookSeventhEG",              50,   0,  100,  5),
    ("EvalConnectedRooksBonus",        10,   0,   40,  3),
    ("EvalKnightMobilityMG",            4,   0,   15,  1),
    ("EvalBishopMobilityMG",            5,   0,   15,  1),
    ("EvalRookMobilityMG",              2,   0,   10,  1),
    ("EvalKnightMobilityEG",            2,   0,   10,  1),
    ("EvalBishopMobilityEG",            3,   0,   10,  1),
    ("EvalRookMobilityEG",              2,   0,    8,  1),
    ("EvalQueenMobilityMG",             1,   0,    8,  1),
    ("EvalQueenMobilityEG",             2,   0,    8,  1),
    ("EvalKingAttackerWeightKnight",    2,   0,   10,  1),
    ("EvalKingAttackerWeightBishop",    2,   0,   10,  1),
    ("EvalKingAttackerWeightRook",      4,   0,   15,  1),
    ("EvalKingAttackerWeightQueen",     8,   0,   20,  2),
    ("EvalPawnShieldBonus",            12,   0,   40,  3),
    ("EvalKingOpenFilePenalty",        20,   0,   60,  4),
    ("EvalKingOpenFileFullExtra",      10,   0,   40,  3),
    ("EvalCastledBonusMG",             20,   0,   60,  3),
    ("EvalTempoBonus",                 10,   0,   30,  2),
    ("EvalCastlingUrgencyPenalty",     20,   0,   40,  3),
    ("EvalThreatByPawnMG",             40,   0,  100,  5),
    ("EvalThreatByMinorMG",            20,   0,   60,  4),
    ("EvalThreatByRookMG",             10,   0,   40,  3),
    ("EvalThreatByPawnEG",             25,   0,   60,  4),
    ("EvalThreatByMinorEG",            15,   0,   40,  3),
    ("EvalThreatByRookEG",              8,   0,   30,  2),
    ("EvalWeakMinorPenaltyMG",        -15, -60,    0,  3),
    ("EvalWeakMinorPenaltyEG",        -10, -40,    0,  2),
    ("EvalSpaceBonusMG",                4,   0,   15,  1),
    ("EvalRookBehindPasserMG",         15,   0,   50,  3),
    ("EvalRookBehindPasserEG",         25,   0,   80,  5),
    ("EvalKingPasserSupportEG",         5,   0,   20,  2),
    ("EvalKingPasserThreatEG",          3,   0,   15,  1),
    ("EvalKnightOutpostSupportedMG",   25,   0,   60,  4),
    ("EvalKnightOutpostSupportedEG",   15,   0,   40,  3),
    ("EvalBishopOutpostSupportedMG",   15,   0,   40,  3),
    ("EvalBishopOutpostSupportedEG",   10,   0,   30,  2),
    ("EvalPinnedPiecePenaltyMG",      -18, -60,    0,  3),
    ("EvalPinnedPiecePenaltyEG",      -10, -40,    0,  2),
    ("EvalPinCreationBonusMG",         12,   0,   50,  3),
    ("EvalPinCreationBonusEG",          6,   0,   30,  2),
    ("EvalBadBishopPerPawnMG",         -4, -20,    0,  1),
    ("EvalBadBishopPerPawnEG",         -6, -20,    0,  1),
    ("EvalConnectedPawnBonusMG",        7,   0,   25,  2),
    ("EvalConnectedPawnBonusEG",        5,   0,   20,  2),
    ("EvalBackwardPawnPenaltyMG",     -12, -40,    0,  2),
    ("EvalBackwardPawnPenaltyEG",      -8, -30,    0,  2),
]

PARAM_DEFAULT = {p[0]: p[1] for p in TUNABLE_PARAMS}
PARAM_STEP    = {p[0]: p[4] for p in TUNABLE_PARAMS}
PARAM_MIN     = {p[0]: p[2] for p in TUNABLE_PARAMS}
PARAM_MAX     = {p[0]: p[3] for p in TUNABLE_PARAMS}


# ---------------------------------------------------------------------------
# Category â†’ parameter mapping
#
# For each category, lists parameters that probably need changing when
# we fail positions of that type. Direction is "up" (increase magnitude)
# or "down" (decrease magnitude / reduce penalty). Confidence 0-1.
# ---------------------------------------------------------------------------

CATEGORY_TO_PARAMS: dict[str, list[tuple[str, str, float]]] = {
    # (param_name, direction, confidence)

    "passed_pawns": [
        ("EvalPassedPawnBonusR3",  "up", 0.8),
        ("EvalPassedPawnBonusR4",  "up", 0.8),
        ("EvalPassedPawnBonusR5",  "up", 0.9),
        ("EvalPassedPawnBonusR6",  "up", 0.9),
        ("EvalPassedPawnEGR4",     "up", 0.85),
        ("EvalPassedPawnEGR5",     "up", 0.85),
        ("EvalPassedPawnEGR6",     "up", 0.9),
        ("EvalCandidatePasserMG",  "up", 0.6),
        ("EvalRookBehindPasserEG", "up", 0.7),
        ("EvalKingPasserSupportEG","up", 0.7),
    ],

    "pawn_structure": [
        ("EvalDoubledPawnPenalty",     "down", 0.7),   # more negative
        ("EvalIsolatedPawnPenalty",    "down", 0.7),
        ("EvalBackwardPawnPenaltyMG",  "down", 0.6),
        ("EvalBackwardPawnPenaltyEG",  "down", 0.6),
        ("EvalConnectedPawnBonusMG",   "up",   0.65),
        ("EvalConnectedPawnBonusEG",   "up",   0.65),
    ],

    "open_files": [
        ("EvalRookOpenFileBonus",  "up", 0.8),
        ("EvalRookSemiOpenBonus",  "up", 0.75),
        ("EvalRookOpenFileEG",     "up", 0.75),
        ("EvalRookSemiOpenEG",     "up", 0.7),
        ("EvalRookSeventhMG",      "up", 0.7),
        ("EvalRookSeventhEG",      "up", 0.7),
        ("EvalConnectedRooksBonus","up", 0.6),
    ],

    "knight_outposts": [
        ("EvalKnightOutpostSupportedMG", "up", 0.9),
        ("EvalKnightOutpostSupportedEG", "up", 0.85),
        ("EvalBishopOutpostSupportedMG", "up", 0.6),
        ("EvalKnightMobilityMG",         "up", 0.55),
    ],

    "king_safety": [
        ("EvalKingAttackerWeightQueen",  "up", 0.85),
        ("EvalKingAttackerWeightRook",   "up", 0.75),
        ("EvalKingAttackerWeightKnight", "up", 0.65),
        ("EvalPawnShieldBonus",          "up", 0.8),
        ("EvalKingOpenFilePenalty",      "up", 0.8),
        ("EvalKingOpenFileFullExtra",    "up", 0.7),
        ("EvalCastledBonusMG",           "up", 0.7),
        ("EvalCastlingUrgencyPenalty",   "up", 0.6),
    ],

    "pins": [
        ("EvalPinnedPiecePenaltyMG", "down", 0.85),   # more negative
        ("EvalPinnedPiecePenaltyEG", "down", 0.8),
        ("EvalPinCreationBonusMG",   "up",   0.75),
        ("EvalPinCreationBonusEG",   "up",   0.7),
    ],

    "bishop_pair": [
        ("EvalBishopPairBonus",   "up", 0.9),
        ("EvalBishopPairEGBonus", "up", 0.85),
        ("EvalBadBishopPerPawnMG","down", 0.65),
        ("EvalBadBishopPerPawnEG","down", 0.65),
    ],

    "mobility": [
        ("EvalKnightMobilityMG", "up", 0.75),
        ("EvalBishopMobilityMG", "up", 0.75),
        ("EvalRookMobilityMG",   "up", 0.7),
        ("EvalQueenMobilityMG",  "up", 0.65),
        ("EvalKnightMobilityEG", "up", 0.7),
        ("EvalBishopMobilityEG", "up", 0.7),
        ("EvalRookMobilityEG",   "up", 0.65),
        ("EvalSpaceBonusMG",     "up", 0.6),
    ],

    "tactical": [
        ("EvalThreatByPawnMG",  "up", 0.8),
        ("EvalThreatByMinorMG", "up", 0.75),
        ("EvalThreatByRookMG",  "up", 0.7),
        ("EvalThreatByPawnEG",  "up", 0.75),
        ("EvalThreatByMinorEG", "up", 0.7),
        ("EvalWeakMinorPenaltyMG", "down", 0.65),
    ],

    "piece_activity": [
        ("EvalBishopMobilityMG",  "up", 0.7),
        ("EvalKnightMobilityMG",  "up", 0.7),
        ("EvalRookMobilityMG",    "up", 0.65),
        ("EvalTempoBonus",        "up", 0.6),
    ],

    "positional": [
        ("EvalTempoBonus",       "up", 0.5),
        ("EvalSpaceBonusMG",     "up", 0.5),
        ("EvalConnectedPawnBonusMG", "up", 0.4),
    ],
}


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _load_diagnosed(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _agreement_rate(results: list[dict]) -> float:
    valid = [r for r in results if "error" not in r and r.get("sf_bestmove")]
    if not valid:
        return 0.0
    ok = sum(1 for r in valid if r.get("agrees_with_sf"))
    return ok / len(valid)


def _phase_stats(results: list[dict]) -> dict:
    """Returns per-phase agreement rate and count."""
    phases = defaultdict(lambda: {"total": 0, "agree": 0, "disagree_cats": defaultdict(int)})
    for r in results:
        if "error" in r or not r.get("sf_bestmove"):
            continue
        ph = r.get("phase", "?")
        phases[ph]["total"] += 1
        if r.get("agrees_with_sf"):
            phases[ph]["agree"] += 1
        else:
            for cat in r.get("categories", []):
                phases[ph]["disagree_cats"][cat] += 1
    return dict(phases)


def _category_stats(results: list[dict]) -> dict:
    """Count of positions per category, and failure rate."""
    stats = defaultdict(lambda: {"total": 0, "fail": 0})
    for r in results:
        if "error" in r or not r.get("sf_bestmove"):
            continue
        for cat in r.get("categories", []):
            stats[cat]["total"] += 1
            if not r.get("agrees_with_sf"):
                stats[cat]["fail"] += 1
    return dict(stats)


def _depth_stats(results: list[dict]) -> dict:
    """At what depth do we first agree with SF? Bucketed."""
    buckets = {"never": 0, "d1-10": 0, "d11-15": 0, "d16-20": 0, "d21-25": 0, "d26+": 0}
    for r in results:
        if "error" in r or not r.get("sf_bestmove"):
            continue
        if not r.get("agrees_with_sf"):
            d = r.get("first_sf_depth")
            if d is None:
                buckets["never"] += 1
            elif d <= 10:
                buckets["d1-10"] += 1
            elif d <= 15:
                buckets["d11-15"] += 1
            elif d <= 20:
                buckets["d16-20"] += 1
            elif d <= 25:
                buckets["d21-25"] += 1
            else:
                buckets["d26+"] += 1
    return buckets


# ---------------------------------------------------------------------------
# Parameter candidate generation
# ---------------------------------------------------------------------------

@dataclass
class ParamCandidate:
    name:            str
    current_default: int
    direction:       str          # "up" or "down"
    suggested_delta: int          # absolute change in centipawns
    suggested_value: int          # clamped to [min, max]
    score:           float        # weighted evidence score
    evidence_cats:   list[str]    # which categories motivated this


def build_param_candidates(cat_stats: dict, total_failures: int) -> list[ParamCandidate]:
    """
    For each parameter, accumulate evidence from each failed category,
    weighted by (failure_rate Ã— confidence Ã— frequency).
    Return ranked list of candidates.
    """
    if total_failures == 0:
        return []

    # Accumulate weighted score per (param, direction)
    evidence: dict[str, dict] = {}  # param â†’ {up: score, down: score, cats: set}

    for cat, mapping in CATEGORY_TO_PARAMS.items():
        if cat not in cat_stats:
            continue
        cs = cat_stats[cat]
        if cs["total"] == 0:
            continue
        fail_rate  = cs["fail"] / cs["total"]
        frequency  = cs["total"] / max(total_failures, 1)
        cat_weight = fail_rate * frequency

        for param_name, direction, confidence in mapping:
            if param_name not in PARAM_DEFAULT:
                continue
            if param_name not in evidence:
                evidence[param_name] = {"up": 0.0, "down": 0.0, "cats": set()}
            evidence[param_name][direction] += cat_weight * confidence
            evidence[param_name]["cats"].add(cat)

    candidates: list[ParamCandidate] = []
    for param, ev in evidence.items():
        total_score = ev["up"] + ev["down"]
        if total_score < 0.05:
            continue
        direction = "up" if ev["up"] >= ev["down"] else "down"
        score     = max(ev["up"], ev["down"])
        default   = PARAM_DEFAULT[param]
        step      = PARAM_STEP[param]
        # Scale delta with step * score magnitude (1-3 steps)
        n_steps   = max(1, min(3, round(score * 6)))
        delta     = step * n_steps
        if direction == "up":
            suggested = min(PARAM_MAX[param], default + delta)
        else:
            suggested = max(PARAM_MIN[param], default - delta)

        candidates.append(ParamCandidate(
            name             = param,
            current_default  = default,
            direction        = direction,
            suggested_delta  = delta if direction == "up" else -delta,
            suggested_value  = suggested,
            score            = round(score, 4),
            evidence_cats    = sorted(ev["cats"]),
        ))

    return sorted(candidates, key=lambda c: -c.score)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _bar(n: int, total: int, width: int = 20) -> str:
    filled = int(n / max(total, 1) * width)
    return "â–ˆ" * filled + "â–‘" * (width - filled)


def format_report(
    data:          dict,
    results:       list[dict],
    phase_stats:   dict,
    cat_stats:     dict,
    depth_stats:   dict,
    candidates:    list[ParamCandidate],
    top_n:         int = 20,
) -> str:
    lines = []
    tag     = data.get("tag", "analysis")
    tiers   = data.get("tiers_ms", [])
    with_sf = data.get("with_sf", True)
    total   = len([r for r in results if "error" not in r])
    errors  = len([r for r in results if "error" in r])
    valid   = [r for r in results if "error" not in r and r.get("sf_bestmove")]

    agree    = sum(1 for r in valid if r.get("agrees_with_sf"))
    disagree = len(valid) - agree
    acc      = agree / len(valid) * 100 if valid else 0

    lines.append(f"â•”â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    lines.append(f"â•‘  Game Analysis Report â€” {tag}")
    lines.append(f"â•‘  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"â•šâ•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    lines.append("")
    lines.append(f"  Positions probed : {total}  (errors: {errors})")
    lines.append(f"  Tiers (ms)       : {tiers}")
    lines.append(f"  SF comparison    : {'yes' if with_sf else 'no'}")
    if with_sf:
        lines.append(f"  Agreement w/ SF  : {agree}/{len(valid)}  ({acc:.1f}%)")
    lines.append("")

    # â”€â”€ Phase breakdown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    lines.append("â”€â”€ By Game Phase â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
    for ph in ["opening", "middlegame", "endgame"]:
        if ph not in phase_stats:
            continue
        ps    = phase_stats[ph]
        n     = ps["total"]
        ok    = ps["agree"]
        fail  = n - ok
        pct   = ok / n * 100 if n else 0
        lines.append(f"  {ph:12s}  {ok:3d}/{n:3d}  {pct:5.1f}%  {_bar(fail, n)}")
    lines.append("")

    # â”€â”€ Category breakdown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    lines.append("â”€â”€ By Position Category â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
    sorted_cats = sorted(cat_stats.items(), key=lambda kv: -kv[1]["fail"])
    for cat, cs in sorted_cats[:15]:
        n    = cs["total"]
        fail = cs["fail"]
        pct  = fail / n * 100 if n else 0
        lines.append(f"  {cat:28s}  fail {fail:3d}/{n:3d} ({pct:4.0f}%)  {_bar(fail, n, 15)}")
    lines.append("")

    # â”€â”€ Depth analysis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if with_sf and any(depth_stats.values()):
        lines.append("â”€â”€ Depth-of-Agreement (failures only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
        for bucket, cnt in depth_stats.items():
            lines.append(f"  {bucket:8s}  {cnt:4d}  {_bar(cnt, disagree or 1, 15)}")
        lines.append("")
        if depth_stats.get("never", 0) > disagree * 0.3:
            lines.append("  âš   >30% of failures NEVER match SF at any depth â†’")
            lines.append("     Likely an eval horizon issue, not a search issue.")
        elif depth_stats.get("d26+", 0) > disagree * 0.4:
            lines.append("  âš   >40% of failures only match SF at depth 26+ â†’")
            lines.append("     Search extensions or aspiration window may be limiting.")
        lines.append("")

    # â”€â”€ Top failures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    failures = [r for r in valid if not r.get("agrees_with_sf")]
    failures.sort(key=lambda r: abs(r.get("swing_cp") or 0), reverse=True)
    if failures:
        lines.append("â”€â”€ Largest Failures (by eval swing) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
        for r in failures[:10]:
            swing = r.get("swing_cp")
            swing_str = f"{swing:+.0f}cp" if swing is not None else "  n/a"
            cat_str   = ",".join(r.get("categories", []))[:30]
            ours      = r.get("our_move", "?")
            sf_mv     = r.get("sf_bestmove", "?")
            lines.append(
                f"  {r['game_id']:20s}  ply={r.get('ply',0):3d}  "
                f"swing={swing_str:7s}  "
                f"bot={ours}  SF={sf_mv}  [{cat_str}]"
            )
        lines.append("")

    # â”€â”€ Parameter candidates â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if candidates:
        lines.append("â”€â”€ Parameter Candidates (ranked by evidence score) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
        lines.append(f"  {'Parameter':<40s}  {'Now':>6s}  {'Dir':>4s}  {'New':>6s}  "
                     f"{'Score':>6s}  Evidence")
        lines.append(f"  {'-'*40}  {'-'*6}  {'-'*4}  {'-'*6}  {'-'*6}  {'-'*30}")
        for c in candidates[:top_n]:
            arrow = "â–²" if c.direction == "up" else "â–¼"
            cats  = ",".join(c.evidence_cats)[:30]
            lines.append(
                f"  {c.name:<40s}  {c.current_default:>6d}  "
                f" {arrow}    {c.suggested_value:>6d}  "
                f"{c.score:>6.3f}  {cats}"
            )
        lines.append("")
        lines.append(f"  â†’ Full candidates â†’ param_candidates.json")
    else:
        lines.append("â”€â”€ Parameter Candidates â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
        lines.append("  No strong candidates identified (need more data or higher disagreement)")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Aggregate diagnosed positions into a report + param candidates."
    )
    p.add_argument("--input",    required=True, metavar="JSON",
                   help="Path to *_diagnosed.json from game_diagnose.py")
    p.add_argument("--top",      type=int, default=20,
                   help="How many parameter candidates to show (default: 20)")
    p.add_argument("--no-params", action="store_true",
                   help="Skip parameter candidate generation")
    p.add_argument("--md",       metavar="PATH",
                   help="Write markdown report to this file")
    p.add_argument("--out-dir",  default=_OUT_DIR)
    return p


def main():
    args = build_parser().parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    data    = _load_diagnosed(args.input)
    results = data.get("results", [])
    tag     = data.get("tag", "analysis")

    print(f"[report] {len(results)} positions loaded from {args.input}")

    # Context window positions (window_role='context') provide investigative arc
    # but should NOT be counted in agreement rates or parameter scoring â€” they
    # were not independently selected as problematic.
    context_rows = [r for r in results if r.get("window_role", "anchor") == "context"]
    results      = [r for r in results if r.get("window_role", "anchor") != "context"]
    if context_rows:
        print(f"[report] {len(context_rows)} context positions excluded from statistics "
              f"({len(results)} anchors used)")
    ps   = _phase_stats(results)
    cs   = _category_stats(results)
    ds   = _depth_stats(results)
    total_failures = sum(1 for r in results if not r.get("agrees_with_sf") and "error" not in r)

    candidates = [] if args.no_params else build_param_candidates(cs, total_failures)

    report_text = format_report(data, results, ps, cs, ds, candidates, top_n=args.top)
    print(report_text)

    # Write markdown
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write("```\n" + report_text + "\n```\n")
        print(f"[report] Markdown â†’ {args.md}")
    else:
        md_path = os.path.join(args.out_dir, f"{tag}_report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("```\n" + report_text + "\n```\n")

    # Write param candidates JSON
    if not args.no_params and candidates:
        out_path = os.path.join(args.out_dir, f"{tag}_param_candidates.json")
        payload  = {
            "tag":        tag,
            "generated":  datetime.utcnow().isoformat(),
            "total_failures": total_failures,
            "candidates": [
                {
                    "name":            c.name,
                    "current_default": c.current_default,
                    "direction":       c.direction,
                    "suggested_delta": c.suggested_delta,
                    "suggested_value": c.suggested_value,
                    "score":           c.score,
                    "evidence_cats":   c.evidence_cats,
                }
                for c in candidates
            ],
        }
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[report] {len(candidates)} param candidates â†’ {out_path}")
        print(f"\nTo run a tuning trial with the top candidates:")
        print(f"  python tools/genetic_tune.py --engine <exe> "
              f"--checkpoint {out_path.replace('_param_candidates', '_ga_seed')}")


if __name__ == "__main__":
    main()
