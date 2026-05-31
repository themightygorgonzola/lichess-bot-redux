# Design benchmark manifests

This folder holds manifest files that map a design candidate to its benchmark outputs.

Recommended fields per design:

- `name`: unique design id
- `family`: optional grouping, e.g. `gelu`, `relu`, `quant`, `engine_v1`
- `variant`: model variant name
- `status`: e.g. `idea`, `fit-only`, `smoke-trained`, `engine-exported`, `candidate`
- `fit_json`: result from the fit benchmark pipeline
- `search_json`: result from the engine search benchmark pipeline
- `param_count`: optional if not embedded in fit result
- `notes`: freeform notes

Use [tools/compare_design_benchmarks.py](tools/compare_design_benchmarks.py) to turn a manifest into a ranked summary.

Example:

- `python tools/compare_design_benchmarks.py --manifest data/benchmarks/designs/example_manifest.json --output-json results/design_compare/example.json --output-csv results/design_compare/example.csv`

Recommended workflow:

1. Define a design candidate.
2. Run fit-capacity benchmark.
3. If viable, export or integrate it into the engine.
4. Run search benchmark on `nps_core.txt`.
5. Optionally run `nps_stress.txt`.
6. Add both result files to a manifest.
7. Generate a comparison table.