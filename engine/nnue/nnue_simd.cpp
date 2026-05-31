// ============================================================================
// nnue_simd.cpp — AVX2 fast-paths for accumulator ops and forward pass (v5)
//
// v5: FT_SIZE=1536 wide accumulator, SCReLU, large bucketed output network
//
// Compiled with -mavx2 (or /arch:AVX2 on MSVC) — the CMakeLists already
// passes -march=native which enables AVX2 on modern x86-64.
//
// If AVX2 is not available at compile time the scalar fallbacks in
// nnue_network.cpp are used instead.
// ============================================================================

#include "nnue_network.h"
#include "nnue_arch.h"
#include <algorithm>
#include <cstdint>

#if defined(__AVX2__) || (defined(_MSC_VER) && defined(__AVX2__))
#include <immintrin.h>
#endif

namespace Chess::NNUE
{

    // ============================================================================
    // AVX2 column add / sub — FT_SIZE int16 in 16-wide lanes
    // ============================================================================

#if defined(__AVX2__) || (defined(_MSC_VER) && defined(__AVX2__))

    void Network::add_column_avx2(int16_t *acc, const int16_t *col) const
    {
        // 1024 int16 / 16 per ymm = 64 iterations
        for (int i = 0; i < FT_SIZE; i += 16)
        {
            __m256i a = _mm256_load_si256(reinterpret_cast<const __m256i *>(acc + i));
            __m256i c = _mm256_load_si256(reinterpret_cast<const __m256i *>(col + i));
            _mm256_store_si256(reinterpret_cast<__m256i *>(acc + i), _mm256_add_epi16(a, c));
        }
    }

    void Network::sub_column_avx2(int16_t *acc, const int16_t *col) const
    {
        for (int i = 0; i < FT_SIZE; i += 16)
        {
            __m256i a = _mm256_load_si256(reinterpret_cast<const __m256i *>(acc + i));
            __m256i c = _mm256_load_si256(reinterpret_cast<const __m256i *>(col + i));
            _mm256_store_si256(reinterpret_cast<__m256i *>(acc + i), _mm256_sub_epi16(a, c));
        }
    }

    // ============================================================================
    // Horizontal sum of __m256i (8 × int32) → scalar int32
    // ============================================================================

    static inline int32_t hsum_epi32_avx2(__m256i v)
    {
        __m128i lo = _mm256_castsi256_si128(v);
        __m128i hi = _mm256_extracti128_si256(v, 1);
        __m128i s = _mm_add_epi32(lo, hi);                // 4 × int32
        s = _mm_add_epi32(s, _mm_shuffle_epi32(s, 0x4E)); // swap hi64/lo64
        s = _mm_add_epi32(s, _mm_shuffle_epi32(s, 0xB1)); // swap pairs
        return _mm_cvtsi128_si32(s);
    }

    static inline __m256i screlu_u16_exact_avx2(__m256i v)
    {
        const __m256i zero = _mm256_setzero_si256();
        const __m256i qa = _mm256_set1_epi16(QA);
        const __m256i one = _mm256_set1_epi16(1);

        v = _mm256_min_epi16(_mm256_max_epi16(v, zero), qa);
        __m256i sq = _mm256_mullo_epi16(v, v);
        // Exact divide by 255 for 0..65535:
        // floor(x / 255) = ((x + 1) + (x >> 8)) >> 8
        return _mm256_srli_epi16(_mm256_add_epi16(_mm256_add_epi16(sq, one), _mm256_srli_epi16(sq, 8)), 8);
    }

    static inline void screlu_u8_exact_avx2(const int16_t *src, uint8_t *dst, int count)
    {
        const __m256i zero = _mm256_setzero_si256();
        for (int i = 0; i < count; i += 16)
        {
            __m256i div = screlu_u16_exact_avx2(_mm256_load_si256(reinterpret_cast<const __m256i *>(src + i)));
            __m256i packed = _mm256_packus_epi16(div, zero);
            packed = _mm256_permute4x64_epi64(packed, 0xD8);
            _mm_storeu_si128(reinterpret_cast<__m128i *>(dst + i), _mm256_castsi256_si128(packed));
        }
    }

    template <int OutputsPerChunk>
    static inline void l1_exact_u8_i8_split_avx2(const uint8_t *input,
                                                 const int8_t *weights,
                                                 const int32_t *biases,
                                                 uint8_t *output)
    {
        static_assert((L1_SIZE % OutputsPerChunk) == 0, "L1 output chunk must divide L1_SIZE");
        static_assert(OutputsPerChunk == 8, "runtime L1 layout currently packs 8 outputs per tile");

        constexpr int InputChunk = 32;
        constexpr int InputChunks = (2 * FT_SIZE) / InputChunk;
        const __m256i mask0f = _mm256_set1_epi8(0x0F);
        const __m256i ones16 = _mm256_set1_epi16(1);

        for (int j = 0; j < L1_SIZE; j += OutputsPerChunk)
        {
            __m256i acc[OutputsPerChunk];
            for (int k = 0; k < OutputsPerChunk; ++k)
            {
                acc[k] = _mm256_setzero_si256();
            }

            const int tile = j / OutputsPerChunk;
            const int8_t *tile_weights = weights + tile * InputChunks * OutputsPerChunk * InputChunk;

            for (int i = 0; i < 2 * FT_SIZE; i += InputChunk)
            {
                __m256i in8 = _mm256_load_si256(reinterpret_cast<const __m256i *>(input + i));
                __m256i in_lo = _mm256_and_si256(in8, mask0f);
                __m256i in_hi = _mm256_and_si256(_mm256_srli_epi16(in8, 4), mask0f);
                const int chunk = i / InputChunk;
                const int8_t *chunk_weights = tile_weights + chunk * OutputsPerChunk * InputChunk;

                for (int k = 0; k < OutputsPerChunk; ++k)
                {
                    const int8_t *w = chunk_weights + k * InputChunk;
                    __m256i w8 = _mm256_load_si256(reinterpret_cast<const __m256i *>(w));
                    __m256i prod_lo = _mm256_maddubs_epi16(in_lo, w8);
                    __m256i sum_lo = _mm256_madd_epi16(prod_lo, ones16);
                    __m256i prod_hi = _mm256_maddubs_epi16(in_hi, w8);
                    __m256i sum_hi = _mm256_madd_epi16(prod_hi, ones16);
                    __m256i sum = _mm256_add_epi32(sum_lo, _mm256_slli_epi32(sum_hi, 4));
                    acc[k] = _mm256_add_epi32(acc[k], sum);
                }
            }

            for (int k = 0; k < OutputsPerChunk; ++k)
            {
                int32_t sum = biases[j + k] + hsum_epi32_avx2(acc[k]);
                output[j + k] = static_cast<uint8_t>(std::clamp(sum / (QA * QB), 0, 127));
            }
        }
    }

    // ============================================================================
    // AVX2 forward pass (v6): SCReLU + bucketed dense output net + skip13.
    //
    // skip13 (v6 new): after L2, add a residual contribution from L1 directly
    // into L3's pre-activation before CReLU. This is computed alongside the
    // normal L3 path in a single fused loop.
    // ============================================================================

    int Network::forward_avx2(const int16_t *white_acc, const int16_t *black_acc,
                              int perspective, int bucket) const
    {
        static_assert((FT_SIZE % 16) == 0, "FT_SIZE must be divisible by 16");
        static_assert((L1_SIZE % 8) == 0, "L1_SIZE must be divisible by 8");
        static_assert((L1_SIZE % 32) == 0, "L1_SIZE must be divisible by 32");
        static_assert((L2_SIZE % 4) == 0, "L2_SIZE must be divisible by 4");
        static_assert((L2_SIZE % 32) == 0, "L2_SIZE must be divisible by 32");
        static_assert((L3_SIZE % 4) == 0, "L3_SIZE must be divisible by 4");
        static_assert((L3_SIZE % 32) == 0, "L3_SIZE must be divisible by 32");

        const int16_t *friendly = (perspective == 0) ? white_acc : black_acc;
        const int16_t *enemy = (perspective == 0) ? black_acc : white_acc;

        const auto &bk = buckets_[bucket];

        // ── Step 1: exact SCReLU precompute ────────────────────────────────────
        alignas(32) uint8_t l1_input[2 * FT_SIZE];
        screlu_u8_exact_avx2(friendly, l1_input, FT_SIZE);
        screlu_u8_exact_avx2(enemy, l1_input + FT_SIZE, FT_SIZE);

        alignas(32) uint8_t l1_out[L1_SIZE];
        l1_exact_u8_i8_split_avx2<8>(l1_input, l1_runtime_bucket(bucket), bk.l1_bias, l1_out);

        // ── Step 3: dense output layers — generic 4-neuron groups via maddubs ───
        alignas(32) uint8_t l2_out[L2_SIZE];
        alignas(32) uint8_t l3_out[L3_SIZE];

        const __m256i ones16 = _mm256_set1_epi16(1);

        auto dense_layer_u8_i8 = [&](const uint8_t *input, int in_size,
                                     const auto &weights, const int32_t *biases,
                                     uint8_t *output, int out_size)
        {
            for (int j = 0; j < out_size; j += 4)
            {
                __m256i acc0 = _mm256_setzero_si256();
                __m256i acc1 = _mm256_setzero_si256();
                __m256i acc2 = _mm256_setzero_si256();
                __m256i acc3 = _mm256_setzero_si256();
                const int8_t *w0 = weights[j + 0];
                const int8_t *w1 = weights[j + 1];
                const int8_t *w2 = weights[j + 2];
                const int8_t *w3 = weights[j + 3];

                for (int i = 0; i < in_size; i += 32)
                {
                    __m256i in_v = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(input + i));
                    __m256i p0 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(w0 + i)));
                    __m256i p1 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(w1 + i)));
                    __m256i p2 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(w2 + i)));
                    __m256i p3 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(w3 + i)));
                    acc0 = _mm256_add_epi32(acc0, _mm256_madd_epi16(p0, ones16));
                    acc1 = _mm256_add_epi32(acc1, _mm256_madd_epi16(p1, ones16));
                    acc2 = _mm256_add_epi32(acc2, _mm256_madd_epi16(p2, ones16));
                    acc3 = _mm256_add_epi32(acc3, _mm256_madd_epi16(p3, ones16));
                }

                output[j + 0] = (uint8_t)std::clamp((hsum_epi32_avx2(acc0) + biases[j + 0]) / QB, 0, 127);
                output[j + 1] = (uint8_t)std::clamp((hsum_epi32_avx2(acc1) + biases[j + 1]) / QB, 0, 127);
                output[j + 2] = (uint8_t)std::clamp((hsum_epi32_avx2(acc2) + biases[j + 2]) / QB, 0, 127);
                output[j + 3] = (uint8_t)std::clamp((hsum_epi32_avx2(acc3) + biases[j + 3]) / QB, 0, 127);
            }
        };

        dense_layer_u8_i8(l1_out, L1_SIZE, bk.l2_weight, bk.l2_bias, l2_out, L2_SIZE);

        // L3 + skip13 (v6): fused loop -- compute (l3_weight @ l2_out) + (skip13_weight @ l1_out)
        // Both inputs are uint8 [0-127], weights are int8. Uses the same maddubs pattern.
        for (int j = 0; j < L3_SIZE; j += 4)
        {
            __m256i acc0 = _mm256_setzero_si256();
            __m256i acc1 = _mm256_setzero_si256();
            __m256i acc2 = _mm256_setzero_si256();
            __m256i acc3 = _mm256_setzero_si256();

            // L3 part: l3_weight @ l2_out  (L2_SIZE=128 elements, 4 chunks of 32)
            for (int i = 0; i < L2_SIZE; i += 32)
            {
                __m256i in_v = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(l2_out + i));
                __m256i p0 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.l3_weight[j + 0] + i)));
                __m256i p1 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.l3_weight[j + 1] + i)));
                __m256i p2 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.l3_weight[j + 2] + i)));
                __m256i p3 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.l3_weight[j + 3] + i)));
                acc0 = _mm256_add_epi32(acc0, _mm256_madd_epi16(p0, ones16));
                acc1 = _mm256_add_epi32(acc1, _mm256_madd_epi16(p1, ones16));
                acc2 = _mm256_add_epi32(acc2, _mm256_madd_epi16(p2, ones16));
                acc3 = _mm256_add_epi32(acc3, _mm256_madd_epi16(p3, ones16));
            }
            // skip13 part: skip13_weight @ l1_out  (L1_SIZE=256 elements, 8 chunks of 32)
            for (int i = 0; i < L1_SIZE; i += 32)
            {
                __m256i in_v = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(l1_out + i));
                __m256i p0 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.skip13_weight[j + 0] + i)));
                __m256i p1 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.skip13_weight[j + 1] + i)));
                __m256i p2 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.skip13_weight[j + 2] + i)));
                __m256i p3 = _mm256_maddubs_epi16(in_v, _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.skip13_weight[j + 3] + i)));
                acc0 = _mm256_add_epi32(acc0, _mm256_madd_epi16(p0, ones16));
                acc1 = _mm256_add_epi32(acc1, _mm256_madd_epi16(p1, ones16));
                acc2 = _mm256_add_epi32(acc2, _mm256_madd_epi16(p2, ones16));
                acc3 = _mm256_add_epi32(acc3, _mm256_madd_epi16(p3, ones16));
            }
            l3_out[j + 0] = (uint8_t)std::clamp((hsum_epi32_avx2(acc0) + bk.l3_bias[j + 0]) / QB, 0, 127);
            l3_out[j + 1] = (uint8_t)std::clamp((hsum_epi32_avx2(acc1) + bk.l3_bias[j + 1]) / QB, 0, 127);
            l3_out[j + 2] = (uint8_t)std::clamp((hsum_epi32_avx2(acc2) + bk.l3_bias[j + 2]) / QB, 0, 127);
            l3_out[j + 3] = (uint8_t)std::clamp((hsum_epi32_avx2(acc3) + bk.l3_bias[j + 3]) / QB, 0, 127);
        }

        // ── Output — vector dot product over L3 activations ─────────────────────
        int32_t result = bk.out_bias;
        for (int i = 0; i < L3_SIZE; i += 32)
        {
            __m256i in_v = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(l3_out + i));
            __m256i w_v = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(bk.out_weight + i));
            __m256i pair = _mm256_maddubs_epi16(in_v, w_v);
            result += hsum_epi32_avx2(_mm256_madd_epi16(pair, ones16));
        }

        return int(result / QB);
    }

    // ============================================================================
    // Fused accumulator updates — copy + add/sub in a single pass.
    // Eliminates the 4KB memcpy that was previously required per non-king move.
    // ============================================================================

    void Network::apply_update_1a1s_avx2(
        int16_t *__restrict dst, const int16_t *__restrict src,
        int add_feat, int sub_feat) const
    {
        const int16_t *add_col = ft_weights_ + add_feat * FT_SIZE;
        const int16_t *sub_col = ft_weights_ + sub_feat * FT_SIZE;
        for (int i = 0; i < FT_SIZE; i += 16)
        {
            __m256i s = _mm256_load_si256(reinterpret_cast<const __m256i *>(src + i));
            __m256i a = _mm256_load_si256(reinterpret_cast<const __m256i *>(add_col + i));
            __m256i b = _mm256_load_si256(reinterpret_cast<const __m256i *>(sub_col + i));
            _mm256_store_si256(reinterpret_cast<__m256i *>(dst + i),
                               _mm256_add_epi16(_mm256_sub_epi16(s, b), a));
        }
    }

    void Network::apply_update_1a2s_avx2(
        int16_t *__restrict dst, const int16_t *__restrict src,
        int add_feat, int sub1_feat, int sub2_feat) const
    {
        const int16_t *add_col = ft_weights_ + add_feat * FT_SIZE;
        const int16_t *sub1_col = ft_weights_ + sub1_feat * FT_SIZE;
        const int16_t *sub2_col = ft_weights_ + sub2_feat * FT_SIZE;
        for (int i = 0; i < FT_SIZE; i += 16)
        {
            __m256i s = _mm256_load_si256(reinterpret_cast<const __m256i *>(src + i));
            __m256i a = _mm256_load_si256(reinterpret_cast<const __m256i *>(add_col + i));
            __m256i b1 = _mm256_load_si256(reinterpret_cast<const __m256i *>(sub1_col + i));
            __m256i b2 = _mm256_load_si256(reinterpret_cast<const __m256i *>(sub2_col + i));
            _mm256_store_si256(reinterpret_cast<__m256i *>(dst + i),
                               _mm256_sub_epi16(_mm256_add_epi16(_mm256_sub_epi16(s, b1), a), b2));
        }
    }

#else
    // Stubs when AVX2 is not available — should never be called at runtime
    // because the evaluate() dispatcher checks the preprocessor flag.
    void Network::add_column_avx2(int16_t *, const int16_t *) const {}
    void Network::sub_column_avx2(int16_t *, const int16_t *) const {}
    int Network::forward_avx2(const int16_t *, const int16_t *, int, int) const { return 0; }
    void Network::apply_update_1a1s_avx2(int16_t *, const int16_t *, int, int) const {}
    void Network::apply_update_1a2s_avx2(int16_t *, const int16_t *, int, int, int) const {}
#endif

} // namespace Chess::NNUE
