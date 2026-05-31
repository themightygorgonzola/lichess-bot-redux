#pragma once
// ============================================================================
// NNUE Network v6 -- HalfKAv2 + SCReLU + skip13 + PSQT + Output Buckets
//
// Architecture (v6):
//   FT (40960 -> 1536) [SCReLU] + PSQT (40960 -> 8, parallel)
//     -> concat(3072)
//     -> L1(256) [CReLU] -> L2(128) [CReLU] -> L3(64)+skip13 [CReLU] -> 1   x8 buckets
//
// Weight storage:
//   - FT:    int16[INPUT_SIZE][FT_SIZE] + int16[FT_SIZE]
//   - PSQT:  int16[INPUT_SIZE][PSQT_BUCKETS]  (new in v6)
//   - Per-bucket output:
//       L1:     int8[L1_SIZE][2*FT_SIZE] + int32[L1_SIZE]
//       L2:     int8[L2_SIZE][L1_SIZE]   + int32[L2_SIZE]
//       skip13: int8[L3_SIZE][L1_SIZE]   (new in v6; projects L1->L3 pre-act)
//       L3:     int8[L3_SIZE][L2_SIZE]   + int32[L3_SIZE]
//       Out:    int8[L3_SIZE]            + int32[1]
//
// Forward pass implemented in both scalar and AVX2 variants.
// ============================================================================

#include "nnue_arch.h"
#include "nnue_accumulator.h"
#include "../types.h"
#include <string>
#include <cstdint>
#include <array>
#include <memory>
#include <new>

namespace Chess
{
    class Board;
}

namespace Chess::NNUE
{

    // Per-bucket output network weights
    struct OutputBucket
    {
        // L1 weights stored in column-block layout for sparse SIMD:
        //   l1_weight_col[b][j*4 + k] = weight[output=j][input=b*4+k]
        //   b = input_block (0..N_L1_BLOCKS-1), j = output neuron, k = in-block offset (0..3)
        static constexpr int N_L1_BLOCKS = 2 * FT_SIZE / 4; // 768 for FT=1536
        alignas(64) int8_t l1_weight_col[N_L1_BLOCKS][L1_SIZE * 4];
        alignas(32) int32_t l1_bias[L1_SIZE];

        alignas(64) int8_t l2_weight[L2_SIZE][L1_SIZE];
        alignas(16) int32_t l2_bias[L2_SIZE];

        // skip13 (v6): residual projection from L1 output -> L3 pre-activation.
        // Input: l1_out (uint8[L1_SIZE]), output: int32 added to L3 pre-act before CReLU.
        // Quantised at QB (same scale as l3_weight).
        alignas(32) int8_t skip13_weight[L3_SIZE][L1_SIZE];

        alignas(32) int8_t l3_weight[L3_SIZE][L2_SIZE];
        alignas(16) int32_t l3_bias[L3_SIZE];

        alignas(16) int8_t out_weight[L3_SIZE];
        int32_t out_bias;
    };

    class Network
    {
    public:
        Network();
        ~Network();

        // ── I/O ──
        bool load(const std::string &path);
        bool save(const std::string &path) const;
        bool is_loaded() const { return loaded_; }

        // ── Accumulator management ──

        // Full refresh: recompute accumulator from board using HalfKAv2 features
        void refresh(Accumulator &acc, const Board &board) const;

        // Incremental ops (single feature add/sub per perspective)
        void add_feature(Accumulator &acc, int perspective, int feature_idx) const;
        void sub_feature(Accumulator &acc, int perspective, int feature_idx) const;

        // Fused FT accumulator updates: copy + delta in one AVX2 pass (no memcpy)
        void apply_update_1a1s(int16_t *dst, const int16_t *src,
                               int add_feat, int sub_feat) const;
        void apply_update_1a2s(int16_t *dst, const int16_t *src,
                               int add_feat, int sub1_feat, int sub2_feat) const;

        // PSQT accumulator updates (v6): scalar, ~8 int32 ops per feature
        void update_psqt_1a1s(int32_t *dst, const int32_t *src,
                              int add_feat, int sub_feat) const;
        void update_psqt_1a2s(int32_t *dst, const int32_t *src,
                              int add_feat, int sub1_feat, int sub2_feat) const;

        // ── Evaluation ──

        // Forward pass: accumulator → SCReLU → bucketed output → centipawns
        // `perspective`: 0=white, 1=black (side to move)
        // `bucket`: output bucket index (from piece_count_bucket)
        int evaluate(const Accumulator &acc, int perspective, int bucket) const;

        // Count pieces on board for bucket selection (convenience)
        static int count_pieces(const Board &board);

    private:
        struct AlignedI8ArrayDeleter
        {
            void operator()(int8_t *p) const noexcept
            {
                if (p)
                    ::operator delete[](p, std::align_val_t(32));
            }
        };
        using AlignedI8Array = std::unique_ptr<int8_t[], AlignedI8ArrayDeleter>;

        static constexpr int L1_INPUT_SIZE = 2 * FT_SIZE;
        static constexpr int L1_OUT_TILE = 8;
        static constexpr int L1_INPUT_CHUNK = 32;

        // ── Feature Transform weights (int16) ──
        // Stored row-major: ft_weights_[feature_idx * FT_SIZE + neuron]
        int16_t *ft_weights_; // heap-allocated: [INPUT_SIZE][FT_SIZE]
        alignas(64) int16_t ft_biases_[FT_SIZE];

        // ── PSQT weights (v6, int16) ──
        // Stored row-major: psqt_weights_[feature_idx * PSQT_BUCKETS + bucket]
        int16_t *psqt_weights_; // heap-allocated: [INPUT_SIZE][PSQT_BUCKETS]

        // ── Output buckets ──
        OutputBucket buckets_[OUTPUT_BUCKETS];
        std::array<AlignedI8Array, OUTPUT_BUCKETS> l1_weight_runtime_;

        bool loaded_ = false;

        void rebuild_runtime_tables();
        const int8_t *l1_runtime_bucket(int bucket) const { return l1_weight_runtime_[bucket].get(); }

        // ── SIMD helpers ──
        void add_column_avx2(int16_t *acc, const int16_t *col) const;
        void sub_column_avx2(int16_t *acc, const int16_t *col) const;
        int forward_avx2(const int16_t *white_acc, const int16_t *black_acc,
                         int perspective, int bucket) const;

        // Fused accumulator updates: copy + delta in one pass (no memcpy)
        void apply_update_1a1s_avx2(int16_t *dst, const int16_t *src,
                                    int add_feat, int sub_feat) const;
        void apply_update_1a2s_avx2(int16_t *dst, const int16_t *src,
                                    int add_feat, int sub1_feat, int sub2_feat) const;

        // Scalar fallbacks
        void add_column_scalar(int16_t *acc, const int16_t *col) const;
        void sub_column_scalar(int16_t *acc, const int16_t *col) const;
        int forward_scalar(const int16_t *white_acc, const int16_t *black_acc,
                           int perspective, int bucket) const;
        void apply_update_1a1s_scalar(int16_t *dst, const int16_t *src,
                                      int add_feat, int sub_feat) const;
        void apply_update_1a2s_scalar(int16_t *dst, const int16_t *src,
                                      int add_feat, int sub1_feat, int sub2_feat) const;
    };

    // Global network instance
    extern Network g_network;

} // namespace Chess::NNUE
