#pragma once
// ============================================================================
// NNUE Architecture Constants — v5 (HalfKAv2 + large quantized output net)
//
// Single shared header for all NNUE components — defines the network
// geometry so that C++ inference and Python training always agree.
//
// Architecture (v5):
//   HalfKAv2 (40960) → FT (1536) [SCReLU] → concat(3072)
//     → L1(256) [CReLU] → L2(128) [CReLU] → L3(64) [CReLU] → 1   ×8 output buckets
//
// MUST match nnue/arch.py exactly.
// ============================================================================

#include <cstdint>

namespace Chess::NNUE
{

    // ── Feature geometry (HalfKAv2) ──────────────────────────────────────────
    //
    // Features per perspective:
    //   index = king_bucket * 640 + rel_color * 320 + piece_type * 64 + square
    //
    //   king_bucket : oriented king square (mirrored if king on files e-h)
    //   rel_color   : 0 = friendly, 1 = enemy
    //   piece_type  : 0=pawn, 1=knight, 2=bishop, 3=rook, 4=queen (no king)
    //   square      : 0..63 (oriented: vertically flipped for black, mirrored if king mirrored)
    //
    // Horizontal mirroring: if the perspective's king is on files e-h,
    // the entire position is mirrored horizontally. This halves the
    // effective feature space.

    constexpr int NUM_PIECE_TYPES = 5; // P N B R Q (no king in features)
    constexpr int NUM_COLORS = 2;      // friendly, enemy
    constexpr int NUM_SQUARES = 64;
    constexpr int NUM_KING_BUCKETS = 64; // one per king square

    constexpr int FEATURES_PER_BUCKET = NUM_COLORS * NUM_PIECE_TYPES * NUM_SQUARES; // 640
    constexpr int INPUT_SIZE = NUM_KING_BUCKETS * FEATURES_PER_BUCKET;              // 40960

    // ── Network geometry ─────────────────────────────────────────────────────

    constexpr int FT_SIZE = 1536;     // Feature transformer hidden size
    constexpr int L1_SIZE = 256;      // First output hidden layer
    constexpr int L2_SIZE = 128;      // Second output hidden layer
    constexpr int L3_SIZE = 64;       // Third output hidden layer
    constexpr int OUTPUT_BUCKETS = 8; // Material-count output heads

    // ── Quantisation ─────────────────────────────────────────────────────────
    // FT: int16, accumulator values in [-32768, 32767]
    // A float activation of 1.0 maps to QA in the accumulator.
    // SCReLU: in Python clamp [0,1] then square → [0,1]
    //   In C++ quantised: clamp [0, QA] then square → [0, QA²]
    //   We divide by QA after squaring to keep the magnitude at [0, QA].
    //
    // Output layers: int8 weights, int32 accumulators.

    constexpr int QA = 255; // FT quantisation scale
    constexpr int QB = 64;  // Output layer weight quantisation scale

    // SCReLU bounds (in quantised accumulator space)
    constexpr int SCRELU_MIN = 0;
    constexpr int SCRELU_MAX = QA; // = 255

    // ── Feature index computation ────────────────────────────────────────────

    // Check if king square needs horizontal mirroring (files e-h = bit 2 set in file)
    inline constexpr bool needs_mirror(int king_sq)
    {
        return (king_sq & 7) >= 4;
    }

    // Mirror a square horizontally (flip file: sq ^= 7)
    inline constexpr int mirror_square(int sq)
    {
        return sq ^ 7;
    }

    // Compute the HalfKAv2 feature index for a single piece.
    //   king_bucket  : oriented king square (already mirrored if needed)
    //   rel_color    : 0=friendly, 1=enemy
    //   piece_type_0 : 0=pawn..4=queen (0-based, no king)
    //   oriented_sq  : piece square (already oriented for perspective + mirror)
    inline constexpr int feature_index(int king_bucket, int rel_color,
                                       int piece_type_0, int oriented_sq)
    {
        return king_bucket * FEATURES_PER_BUCKET + rel_color * (NUM_PIECE_TYPES * NUM_SQUARES) + piece_type_0 * NUM_SQUARES + oriented_sq;
    }

    // ── Output bucket mapping ────────────────────────────────────────────────

    // Map total piece count (2-32) to output bucket index (0-7)
    inline constexpr int piece_count_bucket(int piece_count)
    {
        int b = (piece_count - 2) / 4;
        return (b < 0) ? 0 : (b > 7) ? 7
                                     : b;
    }

    // ── PSQT head (v6) ──────────────────────────────────────────────────────
    // A secondary accumulator per perspective, indexed by the same feature set,
    // with PSQT_BUCKETS outputs per feature.  Accumulates alongside the FT;
    // friendly - enemy contribution is added directly to the eval output.

    constexpr int PSQT_BUCKETS = OUTPUT_BUCKETS; // 8

    // ── File format ──────────────────────────────────────────────────────────

    constexpr uint32_t NNUE_FILE_MAGIC = 0x4E4E5545; // "NNUE"
    constexpr uint32_t NNUE_FILE_VERSION = 6;        // v6 = v5 + skip13 (L1->L3 residual) + PSQT head
    constexpr uint32_t NNUE_FILE_VERSION_V5 = 5;     // kept for fallback loading

} // namespace Chess::NNUE
