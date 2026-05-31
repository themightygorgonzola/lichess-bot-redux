"""
nnue — HalfKAv2 NNUE training package for Redux chess engine.

Architecture:
  HalfKAv2 40960 → 1024 [SCReLU] → (2048) → 8 → 32 → 1  ×8 output buckets

Modules:
  arch      — Constants (geometry, quantisation, file format)
  features  — HalfKAv2 feature encoding (king-relative + horizontal mirror)
  model     — PyTorch NNUE model definition
  dataset   — GPU-optimized data loading from (fen, score, wdl) CSV
  loss      — WDL sigmoid-blend loss function
  trainer   — AMP training loop with checkpointing
  export    — Binary weight export (v2 format for C++ engine)
"""
