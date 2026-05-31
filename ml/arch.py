"""
arch.py — Architecture constants shared between Python training and C++ inference.

These values MUST match src/nnue/nnue_arch.h exactly.
"""

# ── Feature geometry ──────────────────────────────────────────────────────
# HalfKAv2: king_bucket × (color × piece_type × square)
# piece_type: 0=pawn, 1=knight, 2=bishop, 3=rook, 4=queen  (no kings — they ARE the index)
NUM_PIECE_TYPES = 5          # P N B R Q (no king)
NUM_COLORS      = 2
NUM_SQUARES     = 64
NUM_KING_BUCKETS = 64        # one bucket per king square (before mirroring)

# Features per king bucket: color(2) × piece_type(5) × square(64) = 640
FEATURES_PER_BUCKET = NUM_COLORS * NUM_PIECE_TYPES * NUM_SQUARES  # 640

# Total input size per perspective: 64 king buckets × 640 features = 40960
INPUT_SIZE = NUM_KING_BUCKETS * FEATURES_PER_BUCKET  # 40960

# ── Network geometry ──────────────────────────────────────────────────────
# Optimization-target architecture: large ReLU-sized network.
# Hidden sizes mirror the intended large experimental ReLU family while
# retaining the engine's quantized SCReLU/CReLU inference pipeline.
FT_SIZE   = 1536             # Feature transformer hidden size
L1_SIZE   = 256              # First output hidden layer
L2_SIZE   = 128              # Second output hidden layer
L3_SIZE   = 64               # Third output hidden layer
OUTPUT_BUCKETS = 8           # Number of material-count output heads

# ── Quantisation ──────────────────────────────────────────────────────────
QA = 255                     # Feature transformer quantisation scale
QB = 64                      # Output layer quantisation scale
SCRELU_MAX = 1.0             # Python clamp upper bound for SCReLU (= QA in C++ int space)
CRELU_MAX  = 127             # Python/C++ CReLU clamp for L1/L2 output layers

# ── File format ───────────────────────────────────────────────────────────
NNUE_FILE_MAGIC   = 0x4E4E5545   # "NNUE"
NNUE_FILE_VERSION = 6            # v6 = v5 + skip13 (L1->L3 residual) + PSQT head

# v5 version constant (for loading old checkpoints)
NNUE_FILE_VERSION_V5 = 5

# ── PSQT head ──────────────────────────────────────────────────────────────
# Piece-square table head: a separate weight matrix indexed by the same
# HalfKAv2 features, accumulates one scalar per output bucket.  Runs in
# parallel with the main FT; its contribution (friendly − enemy) is added
# directly to the output network's centipawn score.
# No new training data required — derivable from existing feature indices.
PSQT_BUCKETS = OUTPUT_BUCKETS    # = 8; stored separately for documentation clarity

# ── v6 extended features (change 3: HalfKAv2+, needs dataset regen) ───────
# These constants are here for infrastructure; they become active when
# prep_data_v6.py regenerates the dataset with passed-pawn features.
HALFKAV2_SIZE    = INPUT_SIZE        # 40960, the base HalfKAv2 feature range
PASSED_PAWN_SIZE = 128               # 2 × 64: (rel_color) × (oriented_sq)
INPUT_SIZE_V6    = HALFKAV2_SIZE + PASSED_PAWN_SIZE  # 41088
MAX_FEATS_V6     = 48                # up from 32 (passed pawns add ≤8 per perspective)

# ── Output bucket mapping ─────────────────────────────────────────────────
# Maps piece_count (2..32) to one of 8 buckets.
# Bucket boundaries based on SF: (32-pc)/4 approximately.
# This is computed at runtime but we define the function here for clarity.
def piece_count_bucket(piece_count: int) -> int:
    """Map total piece count (2-32) to output bucket index (0-7)."""
    # Kings always present (2 pieces minimum)
    # Bucket 0 = endgame (2-5 pieces), Bucket 7 = opening (29-32 pieces)
    return min(7, max(0, (piece_count - 2) // 4))

# ── Horizontal mirroring ──────────────────────────────────────────────────
def needs_mirror(king_sq: int) -> bool:
    """True if king is on files e-h (file >= 4), requiring horizontal mirror."""
    return (king_sq & 7) >= 4

def mirror_square(sq: int) -> int:
    """Flip a square horizontally: file = 7 - file, rank unchanged."""
    return sq ^ 7
