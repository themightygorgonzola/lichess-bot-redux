# Search benchmark suites

Format:

- One position per line
- `#` starts a comment
- Each position is `name|fen`

Example:

test_start|rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1

Suggested usage:

- Core stable suite for repeatable NPS checks:
  - `data/benchmarks/search/nps_core.txt`
- Stress suite for wider search-shape coverage:
  - `data/benchmarks/search/nps_stress.txt`

Wrapper examples:

- `python tools/search_bench.py --depth 8 --threads 1`
- `python tools/search_bench.py --suite-file data/benchmarks/search/nps_stress.txt --depth 8 --threads 1`
- `python tools/search_bench.py --engine bot/engine/redux-nnue.exe --eval-file bot/engine/nn_profile.bin --depth 8 --threads 1 --json --profile --output results/search_bench/profile_d8.json`

Notes:

- Keep positions fixed once adopted for comparisons.
- Add new suites instead of mutating historical ones when you want continuity.
- Prefer a mix of opening, middlegame, tactical, strategic, and endgame positions.
- `--profile` adds structured hotspot data to the JSON artifact, including search-cycle shares (`nnue`, `movegen`, `do_move`, `undo_move`, `see`, `gcheck`), pruning counters, and NNUE internal counts/cycles (`refresh`, `forward`, incremental/full updates).
- Use a fixed eval file such as `bot/engine/nn_profile.bin` when comparing optimization changes so search speed differences are not confounded by changing weights.