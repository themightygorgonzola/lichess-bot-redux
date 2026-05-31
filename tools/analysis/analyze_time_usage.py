"""
analyze_time_usage.py

Reads a PGN file (with %eval %depth %time %stop annotations) and
produces a per-move breakdown showing:

  - time spent
  - stop reason
  - eval at stop
  - eval delta to next own move (absolute centipawn swing)
  - verdict: over-allocated / well-spent / should-have-stayed

Aggregates by stop-reason bucket so you can see systemic patterns.

Usage:
  python tools/analyze_time_usage.py [path/to/games.pgn] [--last N]
  python tools/analyze_time_usage.py data/games/2026-04.pgn --last 1
"""

import re
import sys
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# ── Regex helpers ─────────────────────────────────────────────────────────
RE_EVAL  = re.compile(r'%eval\s+(#?-?[\d.]+)')
RE_TIME  = re.compile(r'%time\s+([\d.]+)')
RE_STOP  = re.compile(r'%stop\s+(\S+)')
RE_DEPTH = re.compile(r'%depth\s+(\d+)')
RE_MOVE  = re.compile(
    r'\d+\.\s*(?:\.\.\s*)?'        # move number (possibly "...")
    r'([a-hNBRQKO][^\s{(]*)'       # SAN token
    r'(?:\s*\{([^}]*)\})?'         # optional { annotation }
)

# ── PGN splitter ──────────────────────────────────────────────────────────
def split_games(text):
    chunks = re.split(r'(?=\[Event )', text.strip())
    return [c for c in chunks if c.strip()]

def parse_headers(text):
    h = {}
    for m in re.finditer(r'\[(\w+)\s+"([^"]*)"\]', text):
        h[m.group(1)] = m.group(2)
    return h

# ── Per-game analysis ─────────────────────────────────────────────────────
def parse_game(game_text):
    """
    Returns list of move dicts:
      { ply, san, color, time_s, depth, eval_cp, mate, stop }
    """
    moves = []
    ply = 0
    for m in RE_MOVE.finditer(game_text):
        san   = m.group(1)
        annot = m.group(2) or ''

        t_m = RE_TIME.search(annot)
        d_m = RE_DEPTH.search(annot)
        e_m = RE_EVAL.search(annot)
        s_m = RE_STOP.search(annot)

        eval_cp = None
        mate    = None
        if e_m:
            raw = e_m.group(1)
            if raw.startswith('#'):
                mate = int(raw[1:])
            else:
                eval_cp = float(raw) * 100  # stored as pawns → cp

        moves.append({
            'ply'    : ply,
            'san'    : san,
            'color'  : 'white' if ply % 2 == 0 else 'black',
            'time_s' : float(t_m.group(1)) if t_m else None,
            'depth'  : int(d_m.group(1))   if d_m else None,
            'eval_cp': eval_cp,
            'mate'   : mate,
            'stop'   : s_m.group(1)        if s_m else None,
        })
        ply += 1

    return moves


def eval_numeric(mv, sign=1):
    """Centipawn value (large positive = good for white * sign)."""
    if mv.get('mate') is not None:
        return math.copysign(100_000, mv['mate'])   # treat as huge
    if mv.get('eval_cp') is not None:
        return mv['eval_cp']
    return None


BUDGET_STOPS = {'budget', 'budget50', 'budget65', 'timeout'}
EASY_STOPS   = {'confident', 'mate_found'}

# Threshold: if eval swings < this between own moves it was an "easy" position
EASY_DELTA_CP = 50    # < 0.5 pawns swing = settled
HARD_DELTA_CP = 150   # > 1.5 pawns swing = genuinely complex


def analyze_game(moves, bot_color):
    """
    For each bot move, compute:
      - eval_delta_next: |eval change to bot's next move|
      - verdict: 'justified' / 'wasted' / 'cut_short' / 'unknown'
    """
    results = []

    bot_moves = [m for m in moves if m['color'] == bot_color and m['time_s'] is not None]

    for i, mv in enumerate(bot_moves):
        e0 = eval_numeric(mv)

        # find next bot move with an eval
        e1 = None
        for j in range(i + 1, len(bot_moves)):
            e1 = eval_numeric(bot_moves[j])
            if e1 is not None:
                break

        delta = abs(e0 - e1) if (e0 is not None and e1 is not None) else None

        stop = mv['stop']
        if stop is None:
            verdict = 'no_stop'
        elif stop in EASY_STOPS:
            # Stopped because we were confident / found mate
            if delta is not None and delta > HARD_DELTA_CP:
                # Confidence was false — position was harder than we thought
                verdict = 'false_confident'
            else:
                verdict = 'good_stop'
        elif stop in BUDGET_STOPS:
            # Time limit applied
            if delta is None:
                verdict = 'budget_unknown'
            elif delta < EASY_DELTA_CP:
                verdict = 'wasted'      # cut off but position was trivial
            elif delta > HARD_DELTA_CP:
                verdict = 'cut_short'   # genuinely needed more time
            else:
                verdict = 'budget_ok'   # borderline — fine
        else:
            verdict = 'other'

        results.append({**mv, 'delta_cp': delta, 'verdict': verdict})

    return results


def format_eval(mv):
    if mv['mate'] is not None:
        return f"M{mv['mate']:+d}"
    if mv['eval_cp'] is not None:
        return f"{mv['eval_cp']/100:+.2f}"
    return "    ?"


def print_game_report(headers, results, verbose=False):
    gid    = headers.get('GameId',      '?')
    white  = headers.get('White',       '?')
    black  = headers.get('Black',       '?')
    tc     = headers.get('TimeControl', '?')
    result = headers.get('Result',      '?')

    total_s  = sum(r['time_s'] for r in results if r['time_s'])
    n_wasted = sum(1 for r in results if r['verdict'] == 'wasted')
    n_short  = sum(1 for r in results if r['verdict'] == 'cut_short')
    n_false  = sum(1 for r in results if r['verdict'] == 'false_confident')
    n_good   = sum(1 for r in results if r['verdict'] == 'good_stop')

    wasted_s = sum(r['time_s'] for r in results if r['verdict'] == 'wasted' and r['time_s'])

    print(f"\n{'═'*70}")
    print(f"  {white} vs {black}  [{tc}]  result={result}  id={gid}")
    print(f"  Bot total engine time: {total_s:.1f}s")
    print(f"  Moves: {len(results)}")
    print(f"  Good stops       : {n_good}")
    print(f"  False-confident  : {n_false}  (confident but next-move eval swung >{HARD_DELTA_CP}cp)")
    print(f"  Budget-wasted    : {n_wasted}  (cut off but position trivial, Δ<{EASY_DELTA_CP}cp) — {wasted_s:.1f}s wasted")
    print(f"  Cut-short        : {n_short}  (budget hit on complex position, Δ>{HARD_DELTA_CP}cp)")
    print(f"{'─'*70}")

    if verbose or n_wasted or n_short or n_false:
        header = f"  {'Ply':>4}  {'San':<8}  {'Time':>6}  {'D':>3}  {'Eval':>7}  {'Δnext':>7}  {'Stop':<14}  Verdict"
        print(header)
        print(f"  {'─'*66}")
        for r in results:
            delta_str = f"{r['delta_cp']/100:+.2f}" if r['delta_cp'] is not None else "    ?"
            highlight = ' ◄' if r['verdict'] in ('wasted', 'cut_short', 'false_confident') else ''
            print(
                f"  {r['ply']:>4}  {r['san']:<8}  "
                f"{r['time_s']:>5.3f}s  "
                f"{(r['depth'] or 0):>3}  "
                f"{format_eval(r):>7}  "
                f"{delta_str:>7}  "
                f"{(r['stop'] or '?'):<14}  "
                f"{r['verdict']}{highlight}"
            )


def aggregate_report(all_results):
    by_verdict = defaultdict(list)
    for r in all_results:
        by_verdict[r['verdict']].append(r)

    total_s = sum(r['time_s'] for r in all_results if r['time_s'])
    print(f"\n{'═'*70}")
    print(f"  AGGREGATE — {len(all_results)} bot moves, {total_s:.1f}s total engine time")
    print(f"{'─'*70}")

    order = ['good_stop', 'false_confident', 'wasted', 'budget_ok', 'cut_short',
             'budget_unknown', 'no_stop', 'other']
    for v in order:
        rows = by_verdict.get(v, [])
        if not rows:
            continue
        t = sum(r['time_s'] for r in rows if r['time_s'])
        pct = 100 * t / total_s if total_s else 0
        deltas = [r['delta_cp'] for r in rows if r['delta_cp'] is not None]
        avg_d = sum(deltas)/len(deltas) if deltas else None
        delta_str = f"avg Δ={avg_d/100:.2f}" if avg_d is not None else ""
        print(f"  {v:<20} {len(rows):>4} moves  {t:>7.1f}s  ({pct:4.1f}%)  {delta_str}")


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('pgn', nargs='?', default=str(ROOT / 'data' / 'games' / '2026-04.pgn'))
    ap.add_argument('--last',    type=int, default=0,  help='Analyse only the last N games')
    ap.add_argument('--verbose', action='store_true',  help='Print every move even if clean')
    ap.add_argument('--bot',     default='Bot',        help='White or Black name that is the bot')
    ap.add_argument('--speed',   default='',           help='Filter by speed class (e.g. bullet, blitz)')
    args = ap.parse_args()

    text   = open(args.pgn, encoding='utf-8').read()
    chunks = split_games(text)
    if args.speed:
        chunks = [c for c in chunks if args.speed.lower() in parse_headers(c).get('Speed','').lower()]
    if args.last:
        chunks = chunks[-args.last:]
    print(f"Analysing {len(chunks)} games{' (speed='+args.speed+')' if args.speed else ''}...")

    all_results = []
    for chunk in chunks:
        headers  = parse_headers(chunk)
        white, black = headers.get('White','?'), headers.get('Black','?')

        # determine which color the bot is
        if args.bot.lower() in white.lower():
            bot_color = 'white'
        elif args.bot.lower() in black.lower():
            bot_color = 'black'
        else:
            bot_color = 'white'   # fallback

        moves   = parse_game(chunk)
        results = analyze_game(moves, bot_color)
        all_results.extend(results)
        print_game_report(headers, results, verbose=args.verbose)

    if len(chunks) > 1:
        aggregate_report(all_results)


if __name__ == '__main__':
    main()
