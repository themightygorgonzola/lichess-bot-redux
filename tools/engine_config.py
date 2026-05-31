"""
engine_config.py — Single source of truth for engine paths and UCI defaults.

Every tool that spawns the engine should import from here instead of
rolling its own path-discovery logic.  This module resolves:

  • ENGINE_PATH  — the compiled binary (bot/engine/redux-nnue.exe)
  • NNUE_PATH    — the neural-network weights file (nn.bin)
  • PROJECT_ROOT — the repository root

All three are resolved once at import time and cached.
Tools may still accept --engine / --eval-file overrides,
but the defaults come from here.
"""

from pathlib import Path
import sys, os

# ── Project root ──────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Engine binary ─────────────────────────────────────────────────────────

_ENGINE_SEARCH_DIRS = [
    PROJECT_ROOT / "bot" / "engine",       # CMake output (canonical)
]

_ENGINE_NAMES = (
    ["redux-nnue.exe"] if sys.platform == "win32"
    else ["redux-nnue.exe", "redux-nnue"]
)

# HCE-only binary names (the DISABLE_NNUE compile target, kept for compatibility).
# Prefer the unified binary with UseNNUE=false when possible.
_HCE_ENGINE_NAMES = (
    ["redux-hce.exe"] if sys.platform == "win32"
    else ["redux-hce.exe", "redux-hce"]
)


def find_engine(hint: str | None = None) -> str:
    """Return the absolute path to the engine binary.

    Search order:
      1. *hint*         — explicit path from --engine CLI arg
      2. bot/engine/    — the canonical CMake output directory

    Raises FileNotFoundError with an actionable message if nothing is found.
    """
    if hint:
        p = Path(hint)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.is_file():
            return str(p)
        raise FileNotFoundError(f"Specified engine not found: {p}")

    for d in _ENGINE_SEARCH_DIRS:
        for name in _ENGINE_NAMES:
            p = d / name
            if p.is_file():
                return str(p)

    raise FileNotFoundError(
        "No engine binary found.\n"
        "  Expected: bot/engine/lichess-bot.exe\n"
        "  Build with: cd build && cmake --build . --config Release\n"
        "  Or pass --engine /path/to/exe explicitly."
    )


# ── NNUE weights ─────────────────────────────────────────────────────────

_NNUE_SEARCH_PATHS = [
    PROJECT_ROOT / "bot" / "engine" / "nn.bin",   # next to binary (preferred)
    PROJECT_ROOT / "nn.bin",                        # legacy location at root
]


def find_nnue(hint: str | None = None) -> str | None:
    """Return the absolute path to the NNUE weights file, or None.

    Search order:
      1. *hint*             — explicit path from --eval-file CLI arg
      2. bot/engine/nn.bin  — next to the binary
      3. <root>/nn.bin      — legacy fallback

    Returns None (no error) if no weights file exists — the engine will
    fall back to HCE.  Prints a warning in that case.
    """
    if hint:
        p = Path(hint)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.is_file():
            return str(p)
        raise FileNotFoundError(f"Specified NNUE file not found: {p}")

    for p in _NNUE_SEARCH_PATHS:
        if p.is_file():
            return str(p)

    print("WARNING: No nn.bin found — engine will run in HCE mode.",
          file=sys.stderr)
    return None


# ── Standard UCI setup commands ──────────────────────────────────────────

def uci_setup_commands(
    *,
    threads: int = 1,
    hash_mb: int = 128,
    eval_file: str | None = None,
    use_nnue: bool = True,
    extra: dict[str, str] | None = None,
) -> list[str]:
    """Return the list of UCI `setoption` commands for a standard session.

    Always sends Hash and Threads.
    EvalFile is sent only when *use_nnue* is True and weights exist.
    When *use_nnue* is False, sends ``setoption name UseNNUE value false`` so
    the engine uses the classical HCE evaluator — EvalParam setoptions are
    then effective.  Use this for all EvalParam tuning tools.
    *extra* is an optional dict of additional name→value options.
    """
    cmds = [
        f"setoption name Hash value {hash_mb}",
        f"setoption name Threads value {threads}",
    ]
    if use_nnue:
        nnue = eval_file or find_nnue()
        if nnue:
            cmds.append(f"setoption name EvalFile value {nnue}")
    else:
        cmds.append("setoption name UseNNUE value false")
    if extra:
        for k, v in extra.items():
            if k not in ("Hash", "Threads", "EvalFile", "UseNNUE"):
                cmds.append(f"setoption name {k} value {v}")
    return cmds


# ── Convenience: pre-resolved defaults (evaluated at import) ─────────────

try:
    ENGINE_PATH = find_engine()
except FileNotFoundError:
    ENGINE_PATH = None

NNUE_PATH = find_nnue()

# HCE_ENGINE_PATH: path to the dedicated HCE-only binary (redux-hce.exe) if it
# exists, otherwise falls back to the unified binary.  Tuning tools that set
# EvalParams should use this + uci_setup_commands(use_nnue=False).
def find_engine_hce() -> str | None:
    """Return path to the HCE-only binary, or None if only the unified binary exists."""
    for d in _ENGINE_SEARCH_DIRS:
        for name in _HCE_ENGINE_NAMES:
            p = d / name
            if p.is_file():
                return str(p)
    return None

HCE_ENGINE_PATH = find_engine_hce() or ENGINE_PATH
