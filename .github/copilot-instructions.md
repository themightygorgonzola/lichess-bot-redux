# LichessBotRedux — Copilot Instructions

## Directory layout
- `engine/` — C++ UCI engine source (board, search, eval, movegen, TT, NNUE inference, Syzygy)
- `bot/` — Node.js Lichess bot client, dashboard, game management
- `ml/` — Python NNUE training pipeline (`python -m ml.trainer`)
- `tools/` — Dev utilities: data prep, analysis, benchmarking, testing
- `data/` — Training data, game PGNs, processed datasets
- `engines/` — Third-party reference engines (Stockfish, Stormphrax, etc.)
- `archives/` — Build history (`build-N/`) and legacy code

## Build system
Always use `make.ps1` for building — never invoke `cmake` directly.

```
.\make.ps1 build        # compile both redux-nnue.exe and redux-hce.exe
.\make.ps1 build-nnue   # rebuild only redux-nnue.exe
.\make.ps1 build-hce    # rebuild only redux-hce.exe
.\make.ps1 test         # smoke tests
.\make.ps1 run          # start bot (NNUE mode)
.\make.ps1 run-hce      # start bot (HCE mode)
.\make.ps1 clean        # wipe build artefacts
```
