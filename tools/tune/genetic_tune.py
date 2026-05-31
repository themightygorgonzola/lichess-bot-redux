#!/usr/bin/env python3
"""
genetic_tune.py — Genetic Algorithm Tournament Tuner for LichessBotRedux.

Maintains a population of individuals, each a complete set of eval parameters.
Each generation:
  1. Run a full round-robin tournament among all individuals
  2. Score each individual by total match points (W=1, D=0.5, L=0)
  3. Keep the top `--elite` individuals unchanged (elitism)
  4. Breed the rest via tournament selection → uniform crossover → Gaussian mutation
  5. Log results, save best params to JSON, repeat forever

The match runner uses the existing uci/ infrastructure — each pair plays 2 games
(colors swapped), so an 8-player round-robin = 28 matchups × 2 games = 56 games
per generation.

Usage:
    # Start a new run (seeds population from current defaults + mutations)
    python tools/genetic_tune.py --engine build/lichess-bot.exe

    # Resume from checkpoint
    python tools/genetic_tune.py --engine build/lichess-bot.exe --checkpoint data/ga/ga_state.json

    # Custom settings
    python tools/genetic_tune.py --engine build/lichess-bot.exe \\
        --pop 8 --games 2 --movetime 50 --elite 2 --workers 4 --mutation-sigma 1.5

    # Single generation only (for testing)
    python tools/genetic_tune.py --engine build/lichess-bot.exe --generations 1

Press Ctrl+C at any time to save the current best and exit.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import math
import os
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/ root
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from uci import UCIEngine, UCIPlayer, TimeControl, MatchRunner, MatchConfig


# ============================================================================
# Tunable parameter definitions
# (name, default, min, max, step)  — step ≈ sensible mutation sigma
# ============================================================================

TUNABLE_PARAMS: list[tuple[str, int, int, int, int]] = [
    # Pawn structure
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
    # Bishop
    ("EvalBishopPairBonus",            30,   0,  100,  5),
    ("EvalBishopPairEGBonus",          45,   0,  100,  5),
    # Rook
    ("EvalRookOpenFileBonus",          25,   0,   60,  4),
    ("EvalRookSemiOpenBonus",          14,   0,   40,  3),
    ("EvalRookOpenFileEG",             15,   0,   40,  3),
    ("EvalRookSemiOpenEG",              8,   0,   25,  2),
    ("EvalRookSeventhMG",              30,   0,   80,  5),
    ("EvalRookSeventhEG",              50,   0,  100,  5),
    ("EvalConnectedRooksBonus",        10,   0,   40,  3),
    # Mobility
    ("EvalKnightMobilityMG",            4,   0,   15,  1),
    ("EvalBishopMobilityMG",            5,   0,   15,  1),
    ("EvalRookMobilityMG",              2,   0,   10,  1),
    ("EvalKnightMobilityEG",            2,   0,   10,  1),
    ("EvalBishopMobilityEG",            3,   0,   10,  1),
    ("EvalRookMobilityEG",              2,   0,    8,  1),
    ("EvalQueenMobilityMG",             1,   0,    8,  1),
    ("EvalQueenMobilityEG",             2,   0,    8,  1),
    # King safety
    ("EvalKingAttackerWeightKnight",    2,   0,   10,  1),
    ("EvalKingAttackerWeightBishop",    2,   0,   10,  1),
    ("EvalKingAttackerWeightRook",      4,   0,   15,  1),
    ("EvalKingAttackerWeightQueen",     8,   0,   20,  2),
    ("EvalPawnShieldBonus",            12,   0,   40,  3),
    ("EvalKingOpenFilePenalty",        20,   0,   60,  4),
    ("EvalKingOpenFileFullExtra",      10,   0,   40,  3),
    ("EvalCastledBonusMG",             20,   0,   60,  3),
    # Misc positional
    ("EvalTempoBonus",                 10,   0,   30,  2),
    ("EvalCastlingUrgencyPenalty",     20,   0,   40,  3),
    # Threats
    ("EvalThreatByPawnMG",             40,   0,  100,  5),
    ("EvalThreatByMinorMG",            20,   0,   60,  4),
    ("EvalThreatByRookMG",             10,   0,   40,  3),
    ("EvalThreatByPawnEG",             25,   0,   60,  4),
    ("EvalThreatByMinorEG",            15,   0,   40,  3),
    ("EvalThreatByRookEG",              8,   0,   30,  2),
    ("EvalWeakMinorPenaltyMG",        -15, -60,    0,  3),
    ("EvalWeakMinorPenaltyEG",        -10, -40,    0,  2),
    # Space
    ("EvalSpaceBonusMG",                4,   0,   15,  1),
    # Rook behind passer
    ("EvalRookBehindPasserMG",         15,   0,   50,  3),
    ("EvalRookBehindPasserEG",         25,   0,   80,  5),
    # King vs passer
    ("EvalKingPasserSupportEG",         5,   0,   20,  2),
    ("EvalKingPasserThreatEG",          3,   0,   15,  1),
    # Outposts
    ("EvalKnightOutpostSupportedMG",   25,   0,   60,  4),
    ("EvalKnightOutpostSupportedEG",   15,   0,   40,  3),
    ("EvalBishopOutpostSupportedMG",   15,   0,   40,  3),
    ("EvalBishopOutpostSupportedEG",   10,   0,   30,  2),
    # Pins
    ("EvalPinnedPiecePenaltyMG",      -18, -60,    0,  3),
    ("EvalPinnedPiecePenaltyEG",      -10, -40,    0,  2),
    ("EvalPinCreationBonusMG",         12,   0,   50,  3),
    ("EvalPinCreationBonusEG",          6,   0,   30,  2),
    # Bad bishop
    ("EvalBadBishopPerPawnMG",         -4, -20,    0,  1),
    ("EvalBadBishopPerPawnEG",         -6, -20,    0,  1),
    # Connected pawns
    ("EvalConnectedPawnBonusMG",        7,   0,   25,  2),
    ("EvalConnectedPawnBonusEG",        5,   0,   20,  2),
    # Backward pawns
    ("EvalBackwardPawnPenaltyMG",     -12, -40,    0,  2),
    ("EvalBackwardPawnPenaltyEG",      -8, -30,    0,  2),
]

PARAM_NAMES   = [p[0] for p in TUNABLE_PARAMS]
PARAM_DEFAULT = {p[0]: p[1] for p in TUNABLE_PARAMS}
PARAM_MIN     = {p[0]: p[2] for p in TUNABLE_PARAMS}
PARAM_MAX     = {p[0]: p[3] for p in TUNABLE_PARAMS}
PARAM_STEP    = {p[0]: p[4] for p in TUNABLE_PARAMS}


# ============================================================================
# Opening book — diverse positions after ~6-10 moves to reduce white-move bias.
# Games started from these FENs show less first-move advantage and produce more
# decisive results between engines of different strengths.
# ============================================================================

OPENING_BOOK: list[str] = [
    # Sicilian Najdorf 6.Bg5
    "rnbqkb1r/1p2pppp/p2p1n2/6B1/3NP3/2N5/PPP2PPP/R2QKB1R b KQkq - 1 6",
    # Ruy Lopez Berlin
    "r1bqkb1r/pppp1ppp/2n2n2/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
    # King's Indian Samisch
    "rnbq1rk1/ppp1ppbp/3p1np1/8/2PPP3/2N2P2/PP4PP/R1BQKBNR w KQ - 1 6",
    # Queen's Gambit Declined
    "rnbqk2r/ppp1bppp/4pn2/3p4/2PP4/2N2N2/PP2PPPP/R1BQKB1R w KQkq - 2 6",
    # Caro-Kann Classical
    "rnbqkbnr/pp2pppp/2p5/3p4/3PP3/8/PPP2PPP/RNBQKBNR w KQkq - 0 3",
    # French Winawer
    "rnbqk1nr/ppp2ppp/4p3/3p4/1b1PP3/2N5/PPP2PPP/R1BQKBNR w KQkq - 2 5",
    # Nimzo-Indian
    "rnbqk2r/pppp1ppp/4pn2/8/1bPP4/2N5/PP2PPPP/R1BQKBNR w KQkq - 2 4",
    # English Opening
    "rnbqkbnr/pppp1ppp/8/4p3/2P5/2N5/PP1PPPPP/R1BQKBNR b KQkq - 1 2",
    # Dutch Defence
    "rnbqkbnr/ppppp1pp/8/5p2/3P4/8/PPP1PPPP/RNBQKBNR w KQkq f6 0 2",
    # Grunfeld Defence
    "rnbqkb1r/ppp1pp1p/5np1/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq - 0 4",
    # Italian Game
    "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    # Scotch Game
    "r1bqkbnr/pppp1ppp/2n5/4p3/3PP3/5N2/PPP2PPP/RNBQKB1R b KQkq d3 0 3",
    # Pirc Defence
    "rnbqkb1r/ppp1pp1p/3p1np1/8/3PP3/2N5/PPP2PPP/R1BQKBNR w KQkq - 0 4",
    # Benko Gambit
    "rnbqkbnr/p2ppppp/8/1pp5/2PP4/8/PP2PPPP/RNBQKBNR w KQkq c6 0 3",
    # Slav Defence
    "rnbqkb1r/pp2pppp/2p2n2/3p4/2PP4/2N2N2/PP2PPPP/R1BQKB1R b KQkq - 1 5",
    # Catalan Opening
    "rnbqk2r/ppp1ppbp/5np1/3p4/2PP4/5NP1/PP2PPBP/RNBQK2R b KQkq - 0 5",
    # Modern Benoni
    "rnbqkb1r/pp1p1ppp/4pn2/2pP4/2P5/8/PP2PPPP/RNBQKBNR w KQkq - 0 4",
    # Sicilian Dragon
    "rnbqkb1r/pp2pp1p/3p1np1/8/3NP3/2N5/PPP2PPP/R1BQKB1R w KQkq - 0 6",
    # King's Gambit
    "rnbqkbnr/pppp1ppp/8/4p3/4PP2/8/PPPP2PP/RNBQKBNR b KQkq f3 0 2",
    # Closed Sicilian
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/2N5/PPPP1PPP/R1BQKBNR b KQkq - 1 2",
]


# ============================================================================
# Individual — a single member of the population
# ============================================================================

@dataclass
class Individual:
    params: dict[str, int]
    label:  str = ""
    score:  float = 0.0          # tournament score this generation
    wins:   int   = 0
    draws:  int   = 0
    losses: int   = 0
    failed: int   = 0            # games lost to match failures (not scored)

    def clone(self, label: str = "") -> "Individual":
        return Individual(params=copy.deepcopy(self.params), label=label)

    def to_dict(self) -> dict:
        return {"label": self.label, "params": self.params}

    @classmethod
    def from_dict(cls, d: dict) -> "Individual":
        return cls(params=d["params"], label=d.get("label", ""))

    @classmethod
    def from_defaults(cls, label: str = "wildtype") -> "Individual":
        return cls(params=copy.deepcopy(PARAM_DEFAULT), label=label)


# ============================================================================
# Population initialization
# ============================================================================

def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def initialize_population(n: int, rng: random.Random, mutation_sigma: float = 1.5) -> list[Individual]:
    """
    Create n individuals.  Individual 0 is the wild type (current defaults).
    The rest are random mutants seeded from the wild type.
    """
    pop: list[Individual] = []
    wt = Individual.from_defaults(label="gen0-wildtype")
    pop.append(wt)

    for i in range(1, n):
        child = wt.clone(label=f"gen0-mutant{i}")
        for name in PARAM_NAMES:
            sigma = PARAM_STEP[name] * mutation_sigma * 3   # broader initial spread
            v = child.params[name] + int(rng.gauss(0, sigma))
            child.params[name] = clamp(v, PARAM_MIN[name], PARAM_MAX[name])
        pop.append(child)

    return pop


# ============================================================================
# Single match: individual A vs individual B, `games` games
# ============================================================================

# Thread-local print lock so lines don't interleave
_print_lock = threading.Lock()


def _tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs, flush=True)


def run_match(
    engine_path: str,
    ind_a: Individual,
    ind_b: Individual,
    games: int,
    movetime_ms: int,
    hash_mb: int,
    threads: int,
    verbose: bool,
    book_fens: Optional[list[str]] = None,
) -> tuple[float, float, int, int, int]:
    """
    Play `games` games between ind_a (P1) and ind_b (P2).
    Returns (score_a, score_b, wins_a, draws, losses_a).

    When book_fens is provided, games are played in pairs where each pair
    starts from a different book position (cycled), greatly reducing first-move
    bias. games must be even when using book_fens.
    Creates and destroys engine instances internally — fully thread-safe.
    """
    eng_a = eng_b = None
    try:
        eng_a = UCIEngine(engine_path, threads=threads, hash_mb=hash_mb,
                          extra_options=ind_a.params, use_nnue=False)
        eng_b = UCIEngine(engine_path, threads=threads, hash_mb=hash_mb,
                          extra_options=ind_b.params, use_nnue=False)

        p1 = UCIPlayer(eng_a, label=ind_a.label or "A")
        p2 = UCIPlayer(eng_b, label=ind_b.label or "B")

        tc = TimeControl(movetime_ms=movetime_ms)

        total_score_a = 0.0
        total_wins_a  = 0
        total_draws   = 0
        total_losses_a = 0

        if book_fens and games >= 2:
            # Play one 2-game mini-match per book position (cycles if needed)
            pairs = games // 2
            for pair_idx in range(pairs):
                fen = book_fens[pair_idx % len(book_fens)]
                config = MatchConfig(
                    games=2,
                    tc=tc,
                    swap_colors=True,
                    verbose=False,
                    annotate=False,
                    starting_fen=fen,
                )
                result = MatchRunner(p1, p2, config).run()
                total_score_a  += result.score
                total_wins_a   += result.wins
                total_draws    += result.draws
                total_losses_a += result.losses
                # new game between pairs
                eng_a.new_game()
                eng_b.new_game()
        else:
            config = MatchConfig(
                games=games,
                tc=tc,
                swap_colors=True,
                verbose=False,
                annotate=False,
            )
            result = MatchRunner(p1, p2, config).run()
            total_score_a  = result.score
            total_wins_a   = result.wins
            total_draws    = result.draws
            total_losses_a = result.losses

        score_b = games - total_score_a

        if verbose:
            _tprint(f"  {ind_a.label:20s} vs {ind_b.label:20s}  "
                    f"score {total_score_a:.1f}-{score_b:.1f}  "
                    f"(W{total_wins_a}/D{total_draws}/L{total_losses_a})")

        return total_score_a, score_b, total_wins_a, total_draws, total_losses_a

    finally:
        for eng in (eng_a, eng_b):
            if eng is not None:
                try:
                    eng.close()
                except Exception:
                    pass


# ============================================================================
# Tournament — full round-robin
# ============================================================================

def run_tournament(
    pop: list[Individual],
    engine_path: str,
    games_per_match: int,
    movetime_ms: int,
    hash_mb: int,
    threads: int,
    max_workers: int,
    verbose: bool,
    book_fens: Optional[list[str]] = None,
) -> list[float]:
    """
    Run a full round-robin among all individuals.
    Returns a list of total scores, one per individual (index matches pop).
    """
    n = len(pop)
    scores = [0.0] * n
    matchups = list(itertools.combinations(range(n), 2))
    total_games = len(matchups) * games_per_match

    _tprint(f"\n  Round-robin: {n} players, {len(matchups)} matchups, "
            f"{total_games} games total (movetime={movetime_ms}ms)\n")

    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for i, j in matchups:
            fut = pool.submit(
                run_match,
                engine_path,
                pop[i], pop[j],
                games_per_match,
                movetime_ms,
                hash_mb,
                threads,
                verbose,
                book_fens,
            )
            futures[fut] = (i, j)

        completed = 0
        for fut in as_completed(futures):
            i, j = futures[fut]
            completed += 1
            try:
                sa, sb, wi, di, li = fut.result()
                scores[i] += sa
                scores[j] += sb
                # use actual W/D/L from MatchResult (not rounded scores)
                pop[i].wins   += wi
                pop[i].draws  += di
                pop[i].losses += li
                pop[j].wins   += li    # j's wins = i's losses
                pop[j].draws  += di
                pop[j].losses += wi    # j's losses = i's wins
            except Exception as exc:
                _tprint(f"\n  [MATCH FAILURE] {pop[i].label} vs {pop[j].label}")
                _tprint(f"  Error: {exc}")
                _tprint("  " + traceback.format_exc().replace("\n", "\n  "))
                _tprint(f"  -> {games_per_match} games NOT scored for either player.\n")
                pop[i].failed += games_per_match
                pop[j].failed += games_per_match
            _tprint(f"  [{completed:3d}/{len(matchups)}] standings so far: "
                    + "  ".join(f"{pop[k].label}={scores[k]:.1f}" for k in range(n)))

    failures = sum(ind.failed for ind in pop)
    if failures:
        _tprint(f"\n  [!] {failures // 2} match(es) failed this generation — scores may be unreliable.")
        _tprint(f"      Failed game counts per player:")
        for ind in pop:
            if ind.failed:
                _tprint(f"        {ind.label}: {ind.failed} games not scored")

    return scores


# ============================================================================
# Genetic operators
# ============================================================================

def tournament_select(pop: list[Individual], scores: list[float], k: int, rng: random.Random) -> Individual:
    """
    Tournament selection: pick k random individuals, return the best-scoring one.
    """
    candidates = rng.sample(range(len(pop)), k=min(k, len(pop)))
    best = max(candidates, key=lambda i: scores[i])
    return pop[best]


def crossover(p1: Individual, p2: Individual, rng: random.Random, label: str = "") -> Individual:
    """
    Uniform crossover: each parameter is inherited from p1 or p2 with equal probability.
    """
    child_params = {}
    for name in PARAM_NAMES:
        child_params[name] = p1.params[name] if rng.random() < 0.5 else p2.params[name]
    return Individual(params=child_params, label=label)


def mutate(ind: Individual, rng: random.Random, sigma: float, mutation_rate: float) -> Individual:
    """
    Gaussian mutation: each parameter mutated with probability `mutation_rate`.
    Sigma is scaled by PARAM_STEP for each parameter.
    """
    child = ind.clone(label=ind.label)
    for name in PARAM_NAMES:
        if rng.random() < mutation_rate:
            noise = int(rng.gauss(0, PARAM_STEP[name] * sigma))
            v = child.params[name] + noise
            child.params[name] = clamp(v, PARAM_MIN[name], PARAM_MAX[name])
    return child


def breed_next_generation(
    pop: list[Individual],
    scores: list[float],
    elite: int,
    rng: random.Random,
    mutation_sigma: float,
    mutation_rate: float,
    generation: int,
) -> list[Individual]:
    """
    Produce the next generation:
      - Keep top `elite` individuals unchanged
      - Fill the rest with crossover + mutation offspring
    """
    n = len(pop)
    ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)

    next_gen: list[Individual] = []

    # Elites — survive unchanged
    for rank, idx in enumerate(ranked[:elite]):
        survivor = pop[idx].clone(label=f"gen{generation}-elite{rank+1}")
        next_gen.append(survivor)

    # Offspring
    k_tournament = max(2, n // 3)   # tournament selection pressure
    while len(next_gen) < n:
        child_idx = len(next_gen)
        parent1 = tournament_select(pop, scores, k_tournament, rng)
        parent2 = tournament_select(pop, scores, k_tournament, rng)
        child   = crossover(parent1, parent2, rng, label=f"gen{generation}-child{child_idx}")
        child   = mutate(child, rng, sigma=mutation_sigma, mutation_rate=mutation_rate)
        next_gen.append(child)

    return next_gen


# ============================================================================
# Checkpoint save / load
# ============================================================================

def save_checkpoint(path: str, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def load_checkpoint(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_best_params(path: str, ind: Individual, generation: int, score: float) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    record = {
        "generation": generation,
        "score":      score,
        "label":      ind.label,
        "params":     ind.params,
    }
    with open(path, "w") as f:
        json.dump(record, f, indent=2)


# ============================================================================
# Reporting
# ============================================================================

SEPARATOR = "=" * 72

def print_generation_header(gen: int, pop_size: int) -> None:
    _tprint(f"\n{SEPARATOR}")
    _tprint(f"  GENERATION {gen}   ({pop_size} individuals)")
    _tprint(SEPARATOR)


def print_standings(pop: list[Individual], scores: list[float], gen: int) -> None:
    ranked = sorted(range(len(pop)), key=lambda i: scores[i], reverse=True)
    _tprint(f"\n  Generation {gen} final standings:")
    _tprint(f"  {'Rank':4s}  {'Label':24s}  {'Score':6s}  {'W':3s}  {'D':3s}  {'L':3s}  {'Fail':4s}")
    _tprint(f"  {'-'*4}  {'-'*24}  {'-'*6}  {'-'*3}  {'-'*3}  {'-'*3}  {'-'*4}")
    for rank, i in enumerate(ranked):
        p = pop[i]
        fail_str = f"{p.failed:4d}" if p.failed else "   -"
        _tprint(f"  {rank+1:4d}  {p.label:24s}  {scores[i]:6.1f}  "
                f"{p.wins:3d}  {p.draws:3d}  {p.losses:3d}  {fail_str}")


def print_best_params(ind: Individual) -> None:
    _tprint(f"\n  Best individual: {ind.label}")
    _tprint(f"  {'Parameter':42s}  {'Value':>8s}  {'Default':>8s}  {'Δ':>6s}")
    _tprint(f"  {'-'*42}  {'-'*8}  {'-'*8}  {'-'*6}")
    changed = []
    unchanged = []
    for name in PARAM_NAMES:
        v   = ind.params[name]
        dflt = PARAM_DEFAULT[name]
        if v != dflt:
            changed.append((name, v, dflt))
        else:
            unchanged.append((name, v, dflt))
    for name, v, dflt in changed:
        _tprint(f"  {name:42s}  {v:8d}  {dflt:8d}  {v-dflt:+6d}  *")
    for name, v, dflt in unchanged:
        _tprint(f"  {name:42s}  {v:8d}  {dflt:8d}  {0:+6d}")


def print_param_diff(best: Individual) -> None:
    """Print only the parameters that differ from defaults."""
    diffs = [(n, best.params[n], PARAM_DEFAULT[n])
             for n in PARAM_NAMES if best.params[n] != PARAM_DEFAULT[n]]
    if not diffs:
        _tprint("  (no changes from defaults yet)")
        return
    _tprint(f"\n  Changed params ({len(diffs)}):")
    for name, v, dflt in diffs:
        _tprint(f"    {name:42s}  {dflt:+5d} → {v:+5d}  (Δ {v-dflt:+d})")


# ============================================================================
# Main loop
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genetic Algorithm Tournament Tuner for LichessBotRedux"
    )
    parser.add_argument("--engine",    required=True,     help="Path to engine executable")
    parser.add_argument("--checkpoint", default="data/ga/ga_state.json",
                        help="Path to save/resume state (default: data/ga/ga_state.json)")
    parser.add_argument("--best",      default="data/ga/ga_best.json",
                        help="Path to save best params (default: data/ga/ga_best.json)")
    parser.add_argument("--pop",       type=int, default=8,
                        help="Population size (default: 8)")
    parser.add_argument("--games",     type=int, default=4,
                        help="Games per match pair (default: 4; must be even — pairs are WWBB...")
    parser.add_argument("--movetime",  type=int, default=100,
                        help="Movetime per move in ms (default: 100)")
    parser.add_argument("--hash",      type=int, default=32,
                        help="Hash table MB per engine instance (default: 32)")
    parser.add_argument("--threads",   type=int, default=1,
                        help="UCI Threads per engine process (default: 1; increase for longer movetime)")
    parser.add_argument("--no-book",   action="store_true",
                        help="Disable opening book (start all games from startpos)")
    parser.add_argument("--elite",     type=int, default=2,
                        help="Number of elites that survive unchanged (default: 2)")
    parser.add_argument("--workers",   type=int, default=4,
                        help="Parallel match workers (default: 4; set to cpu_count/2 ish)")
    parser.add_argument("--mutation-sigma",  type=float, default=1.5,
                        help="Mutation strength (sigma multiplier on param step, default: 1.5)")
    parser.add_argument("--mutation-rate",   type=float, default=0.3,
                        help="Probability each param is mutated (default: 0.3)")
    parser.add_argument("--generations", type=int, default=0,
                        help="Stop after N generations (0 = run forever, default: 0)")
    parser.add_argument("--seed",      type=int, default=42,
                        help="RNG seed for reproducibility (default: 42)")
    parser.add_argument("--verbose-matches", action="store_true",
                        help="Print per-match result lines")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore existing checkpoint and start fresh")
    args = parser.parse_args()

    engine_path = os.path.abspath(args.engine)
    if not os.path.isfile(engine_path):
        print(f"ERROR: engine not found: {engine_path}", file=sys.stderr)
        sys.exit(1)

    rng = random.Random(args.seed)

    # ------------------------------------------------------------------
    # Load or initialize state
    # ------------------------------------------------------------------
    start_gen = 1
    pop: list[Individual] = []
    all_time_best: Optional[Individual] = None
    all_time_best_score: float = -1.0
    history: list[dict] = []

    if not args.no_resume:
        state = load_checkpoint(args.checkpoint)
        if state:
            _tprint(f"Resuming from checkpoint: {args.checkpoint}  "
                    f"(generation {state['generation']})\n")
            start_gen = state["generation"] + 1
            pop = [Individual.from_dict(d) for d in state["population"]]
            history = state.get("history", [])
            if state.get("best"):
                all_time_best = Individual.from_dict(state["best"])
                all_time_best_score = state.get("best_score", -1.0)

    if not pop:
        _tprint(f"Initializing fresh population (size={args.pop})")
        pop = initialize_population(args.pop, rng, mutation_sigma=args.mutation_sigma)

    book_fens: Optional[list[str]] = None if args.no_book else OPENING_BOOK

    _tprint(f"Engine : {engine_path}")
    _tprint(f"Pop    : {len(pop)}  |  "
            f"Games/match: {args.games}  |  "
            f"Movetime: {args.movetime}ms  |  "
            f"Workers: {args.workers}  |  "
            f"Threads/engine: {args.threads}")
    _tprint(f"Elite  : {args.elite}  |  "
            f"Mutation σ: {args.mutation_sigma}×step  |  "
            f"Mutation rate: {args.mutation_rate:.0%}")
    _tprint(f"Opening book: {'disabled (startpos)' if args.no_book else f'{len(OPENING_BOOK)} positions'}")

    if args.games % 2 != 0 and not args.no_book:
        _tprint("[WARNING] --games should be even when using the opening book (pairs of WWBB games). "
                "Rounding down to nearest even number.")
        args.games = args.games - 1 if args.games > 1 else 2

    matchups_per_gen = len(list(itertools.combinations(range(len(pop)), 2)))
    total_games_per_gen = matchups_per_gen * args.games
    _tprint(f"Games/gen: {total_games_per_gen}  ({matchups_per_gen} matchups × {args.games} games)")
    _tprint(f"Checkpoint: {args.checkpoint}")
    _tprint(f"Best params: {args.best}")
    _tprint("\nPress Ctrl+C at any time to save and exit.\n")

    # ------------------------------------------------------------------
    # Generation loop
    # ------------------------------------------------------------------
    gen = start_gen
    try:
        while True:
            if args.generations and gen > args.generations + start_gen - 1:
                break

            gen_start = time.time()

            # Reset W/D/L/failed counters
            for ind in pop:
                ind.score = 0.0
                ind.wins = ind.draws = ind.losses = ind.failed = 0

            print_generation_header(gen, len(pop))

            scores = run_tournament(
                pop,
                engine_path,
                games_per_match=args.games,
                movetime_ms=args.movetime,
                hash_mb=args.hash,
                threads=args.threads,
                max_workers=args.workers,
                verbose=args.verbose_matches,
                book_fens=book_fens,
            )

            # Store scores on individuals
            for i, ind in enumerate(pop):
                ind.score = scores[i]

            gen_elapsed = time.time() - gen_start

            print_standings(pop, scores, gen)

            # Best this generation
            best_idx   = max(range(len(pop)), key=lambda i: scores[i])
            best_ind   = pop[best_idx]
            best_score = scores[best_idx]

            _tprint(f"\n  Generation {gen} completed in {gen_elapsed:.1f}s")

            # All-time best
            if best_score > all_time_best_score:
                all_time_best = best_ind.clone(label=best_ind.label)
                all_time_best_score = best_score
                _tprint(f"\n  *** NEW ALL-TIME BEST: {best_ind.label}  score={best_score:.1f} ***")
                print_param_diff(all_time_best)
                save_best_params(args.best, all_time_best, gen, best_score)
                _tprint(f"  Best params saved → {args.best}")

            # History entry
            history.append({
                "generation": gen,
                "elapsed_s":  round(gen_elapsed, 1),
                "best_label": best_ind.label,
                "best_score": best_score,
                "scores":     [round(s, 1) for s in scores],
            })

            # Save checkpoint
            state = {
                "generation": gen,
                "population": [ind.to_dict() for ind in pop],
                "best":       all_time_best.to_dict() if all_time_best else None,
                "best_score": all_time_best_score,
                "history":    history,
                "args":       vars(args),
            }
            save_checkpoint(args.checkpoint, state)
            _tprint(f"  Checkpoint saved → {args.checkpoint}")

            # Breed next generation
            pop = breed_next_generation(
                pop, scores,
                elite=args.elite,
                rng=rng,
                mutation_sigma=args.mutation_sigma,
                mutation_rate=args.mutation_rate,
                generation=gen + 1,
            )

            gen += 1

    except KeyboardInterrupt:
        _tprint(f"\n\n  Interrupted at generation {gen}. Saving...")
        if all_time_best:
            save_best_params(args.best, all_time_best, gen - 1, all_time_best_score)
            _tprint(f"  Best params → {args.best}")
        _tprint("  Done. Re-run with --checkpoint to resume.\n")
        sys.exit(0)

    # Normal exit (--generations reached)
    _tprint(f"\n{SEPARATOR}")
    _tprint(f"  Completed {gen - start_gen} generation(s).")
    if all_time_best:
        _tprint(f"\nAll-time best: {all_time_best.label}  score={all_time_best_score:.1f}")
        print_best_params(all_time_best)
        save_best_params(args.best, all_time_best, gen - 1, all_time_best_score)
        _tprint(f"\nFull best params written to: {args.best}")
    _tprint(SEPARATOR)


if __name__ == "__main__":
    main()
