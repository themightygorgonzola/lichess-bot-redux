"""
sprt.py — Sequential Probability Ratio Test for chess engine evaluation.

Uses a trinomial (W/D/L) model with the LLR formula widely used in engine
testing (cf. cutechess / fishtest).

Hypotheses
----------
  H0: Elo difference is elo0  (e.g. 0 — no improvement)
  H1: Elo difference is elo1  (e.g. 3 — meaningful improvement)

Decision boundaries (default α = β = 0.05):
  LLR >= log((1-β)/α) ≈  2.944  →  H1 accepted (change is beneficial)
  LLR <= log(β/(1-α)) ≈ -2.944  →  H0 accepted (change is no better)
  between bounds                 →  inconclusive (keep playing)

Usage
-----
    from tools.sprt import SPRTState
    sprt = SPRTState(elo0=0, elo1=3)
    sprt.update("win")    # or "draw" or "loss"
    print(sprt.summary())
    conclusion = sprt.conclusion()   # "H1_accepted" | "H0_accepted" | None
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _elo_to_score(elo_diff: float) -> float:
    """Expected score [0,1] for the given Elo difference from white/test side."""
    return 1.0 / (1.0 + 10.0 ** (-elo_diff / 400.0))


def _sprt_llr(wins: int, draws: int, losses: int,
              elo0: float, elo1: float) -> float:
    """
    Trinomial LLR for the standard engine-testing SPRT model.

    We estimate draw probability from empirical data; win/loss proportions
    under each hypothesis are derived from the expected score at elo0 / elo1.

    Returns the current log-likelihood ratio (positive favours H1).
    """
    n = wins + draws + losses
    if n == 0:
        return 0.0

    # Empirical proportions
    d_hat = draws / n

    # Expected scores under each hypothesis
    s0 = _elo_to_score(elo0)  # e.g. 0.5 for elo0=0
    s1 = _elo_to_score(elo1)

    # Win/loss proportions under H0 and H1 given shared draw rate d_hat
    # score = win + draw/2  →  win = score - draw/2
    w0 = s0 - d_hat / 2.0
    l0 = 1.0 - s0 - d_hat / 2.0
    w1 = s1 - d_hat / 2.0
    l1 = 1.0 - s1 - d_hat / 2.0

    # Clamp to avoid log(0)
    eps = 1e-9
    w0 = max(w0, eps); l0 = max(l0, eps)
    w1 = max(w1, eps); l1 = max(l1, eps)

    # LLR sum over outcomes (draws cancel because d0 == d1)
    llr = wins * math.log(w1 / w0) + losses * math.log(l1 / l0)
    return llr


# ---------------------------------------------------------------------------
# SPRTState
# ---------------------------------------------------------------------------

@dataclass
class SPRTState:
    """
    Accumulates match results and computes the running LLR.

    Parameters
    ----------
    elo0 : float
        Elo difference under the null hypothesis (typically 0).
    elo1 : float
        Elo difference under the alternative hypothesis (typically 3–5).
    alpha : float
        False-positive rate (accepting H1 when H0 is true). Default 0.05.
    beta : float
        False-negative rate (accepting H0 when H1 is true). Default 0.05.
    """
    elo0:  float = 0.0
    elo1:  float = 3.0
    alpha: float = 0.05
    beta:  float = 0.05

    wins:   int = field(default=0, init=False)
    draws:  int = field(default=0, init=False)
    losses: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._lo = math.log(self.beta / (1.0 - self.alpha))
        self._hi = math.log((1.0 - self.beta) / self.alpha)

    # --- public API ---------------------------------------------------------

    @property
    def games(self) -> int:
        return self.wins + self.draws + self.losses

    @property
    def llr(self) -> float:
        return _sprt_llr(self.wins, self.draws, self.losses, self.elo0, self.elo1)

    @property
    def lo(self) -> float:
        return self._lo

    @property
    def hi(self) -> float:
        return self._hi

    def update(self, result: Literal["win", "draw", "loss"]) -> None:
        """Record one game result from the *test engine's* perspective."""
        if result == "win":
            self.wins += 1
        elif result == "draw":
            self.draws += 1
        elif result == "loss":
            self.losses += 1
        else:
            raise ValueError(f"result must be 'win', 'draw', or 'loss', got {result!r}")

    def conclusion(self) -> Optional[Literal["H1_accepted", "H0_accepted"]]:
        """
        Returns the SPRT decision, or None if still inconclusive.
        """
        llr = self.llr
        if llr >= self._hi:
            return "H1_accepted"
        if llr <= self._lo:
            return "H0_accepted"
        return None

    def summary(self) -> str:
        """One-line human-readable status line."""
        n = self.games
        llr = self.llr if n > 0 else 0.0
        score = (self.wins + 0.5 * self.draws) / n if n > 0 else 0.0
        elo_est = (
            400.0 * math.log10(score / (1.0 - score)) if 0 < score < 1 else float("nan")
        )
        verdict = self.conclusion()
        verdict_str = f" → {verdict}" if verdict else ""
        return (
            f"LLR: {llr:+.3f} [{self._lo:.3f}, {self._hi:.3f}]"
            f"  W:{self.wins} D:{self.draws} L:{self.losses}  ({n} games)"
            f"  Elo≈{elo_est:+.1f}"
            f"{verdict_str}"
        )
