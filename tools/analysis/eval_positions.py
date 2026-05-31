# -*- coding: utf-8 -*-
"""
eval_positions.py
=================
Runs a set of canonical positions through redux-hce.exe and shows the raw
eval breakdown for each.  The purpose is to cross-check the magnitude of
passer/king-proximity terms against every other eval term in the engine.

Usage:
    python eval_positions.py
"""

import subprocess, time, sys, textwrap
sys.stdout.reconfigure(encoding='utf-8')

ENGINE = r"bot\engine\redux-hce.exe"

# ---------------------------------------------------------------------------
# Positions — grouped by diagnostic intent
# ---------------------------------------------------------------------------
# FEN  |  tag  |  description
POSITIONS = [

    # ── Group 0: material calibration ───────────────────────────────────────
    # These have no special structure.  They let us see that the engine's
    # material scale is what we think it is and that structural bonuses are
    # a small fraction of it.
    ("8/8/8/8/8/8/8/R3K2k w Q - 0 1",
     "CAL-1 K+R vs K",
     "Rook endgame, white king uncastled but rook fully active.  R≈+500cp raw. "
     "King PST and rook open-file distort slightly."),

    ("8/8/8/8/8/8/4r3/R3K2k w Q - 0 1",
     "CAL-2 K+R vs K+R (equal)",
     "Dead-equal material.  Should be within ±30cp of 0 depending on PST."),

    ("8/8/8/8/8/8/4r3/R3K1Nk w Q - 0 1",
     "CAL-3 K+R+N vs K+R",
     "White up a knight (~320cp).  Reference for how much a knight is worth "
     "in pure positional eval."),

    ("8/8/8/8/2b5/8/4r3/R3K2k w Q - 0 1",
     "CAL-4 K+R vs K+R+B",
     "White down a bishop (~330cp).  Opposite of CAL-3."),

    # ── Group 1: single passer — how big is the bonus vs piece values? ───────
    # Pure K+P vs K positions at various ranks so we can read off the raw
    # passed_pawn_eg_bonus contribution before king-proximity is added.

    ("8/8/8/3P4/8/8/8/K6k w - - 0 1",
     "PASS-1 K+P(d5) vs K, kings far",
     "White pawn on rank5, king on a1 (dist≈4 from pawn), black king on h1 "
     "(dist≈5).  Passer bonus alone: rank5 EG=68, plus king-passer terms."),

    ("8/8/8/8/8/3P4/8/K6k w - - 0 1",
     "PASS-2 K+P(d3) vs K, kings far",
     "Rank3 passer — EG bonus = 22.  Sanity check that low-rank passers are "
     "correctly cheap."),

    ("8/8/2P5/8/8/8/8/K6k w - - 0 1",
     "PASS-3 K+P(c6) vs K, kings far",
     "Rank6 passer — EG bonus = 112.  Both kings on a1/h1 so king-proximity "
     "terms dominate as the gap is large."),

    ("8/8/2P5/8/2K5/8/8/7k w - - 0 1",
     "PASS-4 K+P(c6) vs K, white king supports",
     "Rank6 passer, white king on c4 (2 steps away), black king on h1 (6 "
     "steps away).  king_passer_support: (6-2)*5=20; threat: 6*3=18.  Total "
     "king bonus = 38 on top of 112 passer = 150cp just for two pawns."),

    ("8/8/2P5/8/3k4/8/8/K7 w - - 0 1",
     "PASS-5 K+P(c6) vs K(d4 blocking range), white passive",
     "Black king on d4 is 2 steps from c6 stop square c7; white king on a1 "
     "is 5 steps away.  king_passer_support: (6-5)*5=5; threat: 2*3=6.  "
     "Total king: 11.  Passer worth 112+11=123 even though the position is "
     "tablebase draw."),

    # ── Group 2: connected + protected chain — what the bot ACTUALLY builds ─
    # These replicate the structures seen in losing games.

    ("8/8/4p3/3pP3/8/8/8/K6k b - - 0 1",
     "CHAIN-1 K+PP(d5+e6) vs K [black to move / black's chain]",
     "Black has d5+e6 — connected (e6 defended by d5) + e6 is rank6 from "
     "black's perspective (rank3 absolute = rank6 black).  "
     "Wait — let me flip: black pawn on d5=rank4abs, e6=rank3abs.  "
     "From black: d5 is rank4 (bonus eg=38), e6 is rank3 (bonus eg=22). "
     "Connected bonus: +5eg each. Realistic mid-chain value."),

    ("8/8/2p5/2Pp4/8/8/8/K6k b - - 0 1",
     "CHAIN-2 K+PP(c6+d5) vs K [black; c6=rank6 from black, d5=rank4]",
     "c6 from black's perspective = rank3 absolute = rank6 EG bonus 112. "
     "Oops: c6 is the 6th rank (rank 6), which in 0-indexed is rank5=index5. "
     "Black c6: rank_of(c6)=RANK_6=5, mirrored r = 8-5-1=2? Let me just run it."),

    ("8/p7/1p6/8/8/8/8/K6k b - - 0 1",
     "CHAIN-3 K+PP(a7+b6) vs K [black; a7=rank2 from black]",
     "a7 = rank7 absolute (very advanced, black rank2), b6 = rank6 absolute "
     "(black rank3 = EG bonus 112).  b6 supports a7 from behind diagonally? "
     "No — b6 is behind a7 but not diagonal. a7 is supported by nothing. "
     "b6 is not supported by a7 (different file, wrong direction)."),

    # ── Group 3: THE CORE QUESTION — passer chain vs piece ──────────────────
    # Each position is: white down a piece, black has a strong pawn chain.
    # What does the engine say?

    ("8/8/8/2Pp4/8/8/8/K5Qk b - - 0 1",
     "CORE-1 K+Q vs K+PP(c5+d5) — white has queen, black has chain",
     "White queen vs black c5+d5 pawns (rank5 each, side by side — NOT "
     "connected by engine def since they're same rank).  Engine should favour "
     "white heavily.  Checks that Q (900) >> 2x rank5 passer (68 each)."),

    ("8/8/1p6/2p5/8/8/8/K5Qk b - - 0 1",
     "CORE-2 K+Q vs K+PP(c5+b6) — proper connected chain",
     "b6+c5 — c5 supports b6 diagonally from behind (pawn_attacks(WHITE,b6) "
     "= a5+c5, so c5 defends b6). b6 is rank6 EG=112+protected(20)+connected "
     "c5=rank5 EG=68+connected.  Total passer chain: ~200cp EG. "
     "White queen: 900cp. Engine should say +600-700 for white."),

    ("8/8/1p6/2p5/8/8/8/K5rk b - - 0 1",
     "CORE-3 K+R vs K+PP(c5+b6) — rook vs strong connected chain",
     "Rook (500) vs b6+c5 chain (~200cp passer eval).  Engine should favour "
     "white by ~300.  If the engine sees this as close or even, the passer "
     "bonuses are overvalued."),

    ("8/8/1p6/2p5/3k4/8/8/K4R2 b - - 0 1",
     "CORE-4 K+R vs K+PP(c5+b6), black king active (d4)",
     "Same as CORE-3 but black king on d4 (3 steps from b6 promotion path, "
     "2 steps from c5).  Adds king_passer_support for black.  Rook should "
     "still win clearly."),

    ("8/1p6/2p5/8/3k4/8/8/K4R2 b - - 0 1",
     "CORE-5 K+R vs K+PP(c6+b7) both rank6+rank7, black king active",
     "b7=rank7 (EG=180), c6=rank6 (EG=112).  b7 is connected (c6 supports "
     "it if pawn_attacks(BLACK,b7) = a6+c6 → c6 does support b7!).  "
     "Protected passer on b7: 180+20+connected=205. c6: 112+connected=117.  "
     "Total chain: ~320cp EG.  Rook only 500.  This is the danger zone."),

    ("8/1p6/2p5/8/3k4/8/8/K2R4 b - - 0 1",
     "CORE-6 same as CORE-5 but rook on d1 (worse position)",
     "Rook on d1 = rook semi-open file, not behind passer.  Chain might look "
     "even more dangerous."),

    # ── Group 4: the mismatch — king proximity vs. passer value ─────────────
    # Quantify exactly how much a passive king costs vs the passer EG bonus.

    ("8/1p6/2p5/8/8/8/8/K7 b - - 0 1",
     "KPROX-1 K+PP(b7+c6), black king missing (off board impossible)",
     "Pure white K vs black b7+c6 chain, no black king.  Impossible legally "
     "but let's use a distant black king for reference."),

    ("8/1p6/2p5/8/3k4/8/8/7K b - - 0 1",
     "KPROX-2 chain with black king d4 (near), white king h1 (far)",
     "Black king 3 steps from b7, white king 6 steps.  Should show max "
     "king-proximity bonus for black."),

    ("8/1p6/2p5/8/3K4/8/8/7k b - - 0 1",
     "KPROX-3 chain with white king d4 (near), black king h1 (far)",
     "Flipped: white king supports stop squares.  Shows the counterbalancing "
     "king-threat term."),

    ("8/1p6/2p5/8/3k4/8/8/3K4 b - - 0 1",
     "KPROX-4 kings equidistant from chain",
     "Both kings equidistant (~3 steps).  king-proximity terms should cancel, "
     "leaving only raw passer bonuses."),
]


def run_eval(fen: str) -> str:
    """Spawn engine, pipe all input at once, collect eval breakdown."""
    cmds = f"uci\nposition fen {fen}\neval\nquit\n"
    result = subprocess.run(
        [ENGINE],
        input=cmds,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = result.stdout
    # Extract from "Eval breakdown" onward
    idx = output.find("Eval breakdown")
    if idx == -1:
        # Fallback: look for the table border
        idx = output.find("+-")
    if idx == -1:
        return output[-800:] if len(output) > 800 else output
    return output[idx:]


def extract_final_score(text: str) -> str:
    for line in text.splitlines():
        if "Final" in line or "final" in line or "score" in line.lower():
            return line
    return "(see full output)"


def main():
    print("=" * 78)
    print("EVAL POSITION TEST -- redux-hce.exe")
    print("Purpose: compare passer/king-proximity magnitude vs all other terms")
    print("=" * 78)

    for fen, tag, desc in POSITIONS:
        print(f"\n{'-'*78}")
        print(f"[{tag}]")
        print(textwrap.fill(desc, width=76, initial_indent="  ", subsequent_indent="  "))
        print(f"  FEN: {fen}")
        print()
        output = run_eval(fen)
        print(output or "  (no eval output produced)")

    print(f"\n{'='*78}")
    print("Done.")


if __name__ == "__main__":
    main()
