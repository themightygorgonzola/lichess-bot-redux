"""One-shot SeeCaptScale probe — reuses existing SF cache, no recompile needed."""
import sys, json, time
from pathlib import Path
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).parent))
from sf_guided_tune import (
    load_suite, load_or_build_sf_cache, run_fen_detailed,
    compute_loss, read_engine_defaults, filter_positions,
    RESULTS_DIR, ENGINE_DEFAULT, SF_DEFAULT, SUITE_DEFAULT,
    FILTER_THRESH, FILTER_DEPTH,  # now 350 — avoids blindspot that caused b92 over-tune
)

engine     = ENGINE_DEFAULT
cache_path = RESULTS_DIR / "analysis" / "sf_guide_cache.json"
depth      = 12
hash_mb    = 64

positions  = load_suite(SUITE_DEFAULT)
sf_cache   = load_or_build_sf_cache(cache_path, SF_DEFAULT, positions, 16, force_rebuild=True)
positions  = filter_positions(engine, positions, sf_cache, FILTER_DEPTH, FILTER_THRESH)
defaults   = read_engine_defaults(engine)

candidates = [40, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 175, 190, 210, 230, 250, 300]

print(f"\nSeeCaptScale probe  (d{depth}, {len(positions)} positions after filter)")
print(f"{'Value':>8}  {'Total':>8}  {'MvCost':>8}  {'EvalErr':>8}  {'FartΔ':>7}")
print("-" * 52)

best_val, best_loss = None, 1e9
for v in candidates:
    p = deepcopy(defaults)
    p["SeeCaptScale"] = v
    t0 = time.time()
    results = [run_fen_detailed(engine, fen, depth, p, hash_mb, timeout=120)
               for _, fen in positions]
    loss = compute_loss(results, sf_cache, positions)
    marker = " <--" if loss["total"] < best_loss else ""
    if loss["total"] < best_loss:
        best_loss = loss["total"]
        best_val  = v
    print(f"{v:>8}  {loss['total']:>8.4f}  "
          f"{loss.get('mean_move_cost_cp', 0):>7}cp  "
          f"{loss.get('mean_eval_err_cp', 0):>7}cp  "
          f"{loss.get('mean_fart_delta_cp', 0):>6}cp  "
          f"({time.time()-t0:.1f}s){marker}")

print(f"\nBest: SeeCaptScale={best_val}  loss={best_loss:.4f}")
print(f"Current default (b92): 160")
