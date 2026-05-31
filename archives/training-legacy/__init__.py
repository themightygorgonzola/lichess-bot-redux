"""
NNUE Training Pipeline for LichessBotRedux
===========================================

Architecture: 768 → 256 → 1  (perspective network)
  Input:   768 binary features  (2 colors × 6 piece types × 64 squares)
  Hidden:  256 neurons, clipped-ReLU [0, 1]
  Output:  1 scalar (centipawns, side-to-move perspective)

Training data format:
  Each sample is (fen, score_cp) where score_cp is from white's perspective.
  The trainer converts this to the side-to-move perspective internally.

Usage:
  python -m training.train_nnue --data training_data.csv --epochs 100 --lr 0.001
  python -m training.export_weights --checkpoint best_model.pt --output nn.bin
"""
