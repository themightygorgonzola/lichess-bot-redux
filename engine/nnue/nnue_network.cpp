// ============================================================================
// nnue_network.cpp -- Network I/O, accumulator refresh, forward pass (v6)
//
// v6: v5 + skip13 (L1->L3 residual) + PSQT head (parallel accumulator)
// ============================================================================

#include "nnue_network.h"
#include "../board.h"
#include <fstream>
#include <iostream>
#include <cstring>
#include <algorithm>
#include <cmath>

namespace Chess::NNUE
{

    // Global instance
    Network g_network;

    Network::Network()
    {
        // Heap-allocate FT weights: INPUT_SIZE x FT_SIZE x 2 bytes
        ft_weights_ = static_cast<int16_t *>(
            ::operator new[](
                size_t(INPUT_SIZE) * FT_SIZE * sizeof(int16_t),
                std::align_val_t(32)));
        std::memset(ft_weights_, 0, size_t(INPUT_SIZE) * FT_SIZE * sizeof(int16_t));

        // Heap-allocate PSQT weights: INPUT_SIZE x PSQT_BUCKETS x 2 bytes (v6)
        psqt_weights_ = static_cast<int16_t *>(
            ::operator new[](
                size_t(INPUT_SIZE) * PSQT_BUCKETS * sizeof(int16_t),
                std::align_val_t(32)));
        std::memset(psqt_weights_, 0, size_t(INPUT_SIZE) * PSQT_BUCKETS * sizeof(int16_t));

        std::memset(ft_biases_, 0, sizeof(ft_biases_));
        std::memset(buckets_, 0, sizeof(buckets_));
        for (auto &ptr : l1_weight_runtime_)
        {
            ptr.reset(static_cast<int8_t *>(::operator new[](L1_SIZE * L1_INPUT_SIZE * sizeof(int8_t), std::align_val_t(32))));
            std::fill_n(ptr.get(), L1_SIZE * L1_INPUT_SIZE, int8_t(0));
        }
    }

    Network::~Network()
    {
        ::operator delete[](ft_weights_, std::align_val_t(32));
        ::operator delete[](psqt_weights_, std::align_val_t(32));
    }

    void Network::rebuild_runtime_tables()
    {
        static_assert((L1_SIZE % L1_OUT_TILE) == 0, "L1_SIZE must be divisible by L1_OUT_TILE");
        static_assert((L1_INPUT_SIZE % L1_INPUT_CHUNK) == 0, "L1_INPUT_SIZE must be divisible by L1_INPUT_CHUNK");

        constexpr int OUT_TILES = L1_SIZE / L1_OUT_TILE;
        constexpr int IN_CHUNKS = L1_INPUT_SIZE / L1_INPUT_CHUNK;

        for (int b = 0; b < OUTPUT_BUCKETS; ++b)
        {
            int8_t *dst = l1_weight_runtime_[b].get();
            const auto &bk = buckets_[b];
            for (int tile = 0; tile < OUT_TILES; ++tile)
            {
                for (int chunk = 0; chunk < IN_CHUNKS; ++chunk)
                {
                    int8_t *block = dst + (((tile * IN_CHUNKS) + chunk) * L1_OUT_TILE * L1_INPUT_CHUNK);
                    for (int lane = 0; lane < L1_OUT_TILE; ++lane)
                    {
                        const int out_idx = tile * L1_OUT_TILE + lane;
                        for (int t = 0; t < L1_INPUT_CHUNK; ++t)
                        {
                            const int in_idx = chunk * L1_INPUT_CHUNK + t;
                            block[lane * L1_INPUT_CHUNK + t] = static_cast<int8_t>(
                                bk.l1_weight_col[in_idx / 4][out_idx * 4 + (in_idx & 3)]);
                        }
                    }
                }
            }
        }
    }

    // ============================================================================
    // Binary weight file format (v6)
    //
    //   Header (9 x uint32 = 36 bytes):
    //     magic, version, input_size, ft_size, l1_size, l2_size, l3_size,
    //     output_buckets, psqt_buckets
    //
    //   FT biases:    int16[FT_SIZE]
    //   FT weights:   int16[INPUT_SIZE][FT_SIZE]        (row-major)
    //   PSQT weights: int16[INPUT_SIZE][PSQT_BUCKETS]   (row-major, v6 new)
    //
    //   Per bucket (x OUTPUT_BUCKETS):
    //     L1 weights:    int8[L1_SIZE][2*FT_SIZE]  (column-block layout)
    //     L1 biases:     int32[L1_SIZE]
    //     L2 weights:    int8[L2_SIZE][L1_SIZE]
    //     L2 biases:     int32[L2_SIZE]
    //     skip13 weights: int8[L3_SIZE][L1_SIZE]   (v6 new: L1->L3 residual)
    //     L3 weights:    int8[L3_SIZE][L2_SIZE]
    //     L3 biases:     int32[L3_SIZE]
    //     Out weights:   int8[L3_SIZE]
    //     Out bias:      int32
    // ============================================================================

    bool Network::load(const std::string &path)
    {
        std::ifstream f(path, std::ios::binary);
        if (!f)
        {
            std::cerr << "NNUE: cannot open " << path << "\n";
            return false;
        }

        // Read header
        uint32_t magic, version, in_sz, ft_sz, l1_sz, l2_sz, l3_sz, n_buckets, psqt_b;
        f.read(reinterpret_cast<char *>(&magic), 4);
        f.read(reinterpret_cast<char *>(&version), 4);
        f.read(reinterpret_cast<char *>(&in_sz), 4);
        f.read(reinterpret_cast<char *>(&ft_sz), 4);
        f.read(reinterpret_cast<char *>(&l1_sz), 4);
        f.read(reinterpret_cast<char *>(&l2_sz), 4);
        f.read(reinterpret_cast<char *>(&l3_sz), 4);
        f.read(reinterpret_cast<char *>(&n_buckets), 4);
        f.read(reinterpret_cast<char *>(&psqt_b), 4); // v6 new

        if (magic != NNUE_FILE_MAGIC)
        {
            std::cerr << "NNUE: bad magic in " << path
                      << " (got " << std::hex << magic << ")\n";
            return false;
        }
        if (version != NNUE_FILE_VERSION)
        {
            std::cerr << "NNUE: version mismatch in " << path
                      << " (got " << version << ", need " << NNUE_FILE_VERSION << ")\n";
            return false;
        }
        if (int(in_sz) != INPUT_SIZE || int(ft_sz) != FT_SIZE ||
            int(l1_sz) != L1_SIZE || int(l2_sz) != L2_SIZE ||
            int(l3_sz) != L3_SIZE || int(n_buckets) != OUTPUT_BUCKETS ||
            int(psqt_b) != PSQT_BUCKETS)
        {
            std::cerr << "NNUE: architecture mismatch in " << path << "\n";
            return false;
        }

        // Read FT biases and weights
        f.read(reinterpret_cast<char *>(ft_biases_), FT_SIZE * sizeof(int16_t));
        f.read(reinterpret_cast<char *>(ft_weights_),
               (size_t)INPUT_SIZE * FT_SIZE * sizeof(int16_t));

        // Read PSQT weights (v6 new)
        f.read(reinterpret_cast<char *>(psqt_weights_),
               (size_t)INPUT_SIZE * PSQT_BUCKETS * sizeof(int16_t));

        // Read per-bucket output networks
        for (int b = 0; b < OUTPUT_BUCKETS; ++b)
        {
            auto &bk = buckets_[b];
            f.read(reinterpret_cast<char *>(bk.l1_weight_col), sizeof(bk.l1_weight_col));
            f.read(reinterpret_cast<char *>(bk.l1_bias), sizeof(bk.l1_bias));
            f.read(reinterpret_cast<char *>(bk.l2_weight), sizeof(bk.l2_weight));
            f.read(reinterpret_cast<char *>(bk.l2_bias), sizeof(bk.l2_bias));
            f.read(reinterpret_cast<char *>(bk.skip13_weight), sizeof(bk.skip13_weight)); // v6
            f.read(reinterpret_cast<char *>(bk.l3_weight), sizeof(bk.l3_weight));
            f.read(reinterpret_cast<char *>(bk.l3_bias), sizeof(bk.l3_bias));
            f.read(reinterpret_cast<char *>(bk.out_weight), sizeof(bk.out_weight));
            f.read(reinterpret_cast<char *>(&bk.out_bias), sizeof(bk.out_bias));
        }

        if (!f)
        {
            std::cerr << "NNUE: truncated/corrupt file " << path << "\n";
            return false;
        }

        rebuild_runtime_tables();
        loaded_ = true;
        return true;
    }

    bool Network::save(const std::string &path) const
    {
        std::ofstream f(path, std::ios::binary);
        if (!f)
            return false;

        uint32_t vals[] = {
            NNUE_FILE_MAGIC,
            NNUE_FILE_VERSION,
            uint32_t(INPUT_SIZE),
            uint32_t(FT_SIZE),
            uint32_t(L1_SIZE),
            uint32_t(L2_SIZE),
            uint32_t(L3_SIZE),
            uint32_t(OUTPUT_BUCKETS),
            uint32_t(PSQT_BUCKETS),
        };
        f.write(reinterpret_cast<const char *>(vals), sizeof(vals));

        f.write(reinterpret_cast<const char *>(ft_biases_), FT_SIZE * sizeof(int16_t));
        f.write(reinterpret_cast<const char *>(ft_weights_),
                (size_t)INPUT_SIZE * FT_SIZE * sizeof(int16_t));
        f.write(reinterpret_cast<const char *>(psqt_weights_),
                (size_t)INPUT_SIZE * PSQT_BUCKETS * sizeof(int16_t)); // v6

        for (int b = 0; b < OUTPUT_BUCKETS; ++b)
        {
            const auto &bk = buckets_[b];
            f.write(reinterpret_cast<const char *>(bk.l1_weight_col), sizeof(bk.l1_weight_col));
            f.write(reinterpret_cast<const char *>(bk.l1_bias), sizeof(bk.l1_bias));
            f.write(reinterpret_cast<const char *>(bk.l2_weight), sizeof(bk.l2_weight));
            f.write(reinterpret_cast<const char *>(bk.l2_bias), sizeof(bk.l2_bias));
            f.write(reinterpret_cast<const char *>(bk.skip13_weight), sizeof(bk.skip13_weight)); // v6
            f.write(reinterpret_cast<const char *>(bk.l3_weight), sizeof(bk.l3_weight));
            f.write(reinterpret_cast<const char *>(bk.l3_bias), sizeof(bk.l3_bias));
            f.write(reinterpret_cast<const char *>(bk.out_weight), sizeof(bk.out_weight));
            f.write(reinterpret_cast<const char *>(&bk.out_bias), sizeof(bk.out_bias));
        }

        return f.good();
    }

    // ============================================================================
    // Full refresh — build accumulator using HalfKAv2 features
    // ============================================================================

    void Network::refresh(Accumulator &acc, const Board &board) const
    {
        // Start from biases
        for (int p = 0; p < 2; ++p)
            std::memcpy(acc.values[p], ft_biases_, FT_SIZE * sizeof(int16_t));

        // Find king squares
        Square white_king = board.king_square(WHITE);
        Square black_king = board.king_square(BLACK);

        // Compute king buckets for each perspective
        // White perspective: king is already from white's view
        int w_king_oriented = int(white_king);
        bool w_mirror = needs_mirror(w_king_oriented);
        if (w_mirror)
            w_king_oriented = mirror_square(w_king_oriented);

        // Black perspective: flip king vertically, then check mirror
        int b_king_oriented = int(black_king) ^ 56;
        bool b_mirror = needs_mirror(b_king_oriented);
        if (b_mirror)
            b_king_oriented = mirror_square(b_king_oriented);

        Bitboard bb = board.pieces();
        // Zero PSQT accumulators
        std::memset(acc.psqt, 0, sizeof(acc.psqt));
        while (bb)
        {
            Square sq = pop_lsb(bb);
            Piece pc = board.piece_on(sq);
            PieceType pt = type_of(pc);
            if (pt == KING)
                continue; // kings are not features
            Color color = color_of(pc);

            // White perspective
            int w_rel_color = (color == WHITE) ? 0 : 1;
            int w_sq = int(sq);
            if (w_mirror)
                w_sq ^= 7;
            int w_feat = feature_index(w_king_oriented, w_rel_color, int(pt) - 1, w_sq);

            // Black perspective
            int b_rel_color = (color == BLACK) ? 0 : 1;
            int b_sq = int(sq) ^ 56;
            if (b_mirror)
                b_sq ^= 7;
            int b_feat = feature_index(b_king_oriented, b_rel_color, int(pt) - 1, b_sq);

#if defined(__AVX2__)
            add_column_avx2(acc.values[0], &ft_weights_[w_feat * FT_SIZE]);
            add_column_avx2(acc.values[1], &ft_weights_[b_feat * FT_SIZE]);
#else
            add_column_scalar(acc.values[0], &ft_weights_[w_feat * FT_SIZE]);
            add_column_scalar(acc.values[1], &ft_weights_[b_feat * FT_SIZE]);
#endif
            // PSQT update (v6): accumulate 8 int32 values per perspective
            const int16_t *w_psqt = psqt_weights_ + w_feat * PSQT_BUCKETS;
            const int16_t *b_psqt = psqt_weights_ + b_feat * PSQT_BUCKETS;
            for (int bk = 0; bk < PSQT_BUCKETS; ++bk)
            {
                acc.psqt[0][bk] += w_psqt[bk];
                acc.psqt[1][bk] += b_psqt[bk];
            }
        }

        acc.computed = true;
    }

    // ============================================================================
    // Incremental add / sub
    // ============================================================================

    void Network::add_feature(Accumulator &acc, int perspective, int feature_idx) const
    {
#if defined(__AVX2__)
        add_column_avx2(acc.values[perspective], &ft_weights_[feature_idx * FT_SIZE]);
#else
        add_column_scalar(acc.values[perspective], &ft_weights_[feature_idx * FT_SIZE]);
#endif
        // PSQT update (v6)
        const int16_t *row = psqt_weights_ + feature_idx * PSQT_BUCKETS;
        for (int b = 0; b < PSQT_BUCKETS; ++b)
            acc.psqt[perspective][b] += row[b];
    }

    void Network::sub_feature(Accumulator &acc, int perspective, int feature_idx) const
    {
#if defined(__AVX2__)
        sub_column_avx2(acc.values[perspective], &ft_weights_[feature_idx * FT_SIZE]);
#else
        sub_column_scalar(acc.values[perspective], &ft_weights_[feature_idx * FT_SIZE]);
#endif
        // PSQT update (v6)
        const int16_t *row = psqt_weights_ + feature_idx * PSQT_BUCKETS;
        for (int b = 0; b < PSQT_BUCKETS; ++b)
            acc.psqt[perspective][b] -= row[b];
    }

    void Network::apply_update_1a1s(int16_t *dst, const int16_t *src,
                                    int add_feat, int sub_feat) const
    {
#if defined(__AVX2__)
        apply_update_1a1s_avx2(dst, src, add_feat, sub_feat);
#else
        apply_update_1a1s_scalar(dst, src, add_feat, sub_feat);
#endif
    }

    void Network::apply_update_1a2s(int16_t *dst, const int16_t *src,
                                    int add_feat, int sub1_feat, int sub2_feat) const
    {
#if defined(__AVX2__)
        apply_update_1a2s_avx2(dst, src, add_feat, sub1_feat, sub2_feat);
#else
        apply_update_1a2s_scalar(dst, src, add_feat, sub1_feat, sub2_feat);
#endif
    }

    // PSQT fused updates (v6): scalar, 8 int32 ops per feature changed
    void Network::update_psqt_1a1s(int32_t *dst, const int32_t *src,
                                   int add_feat, int sub_feat) const
    {
        const int16_t *add_row = psqt_weights_ + add_feat * PSQT_BUCKETS;
        const int16_t *sub_row = psqt_weights_ + sub_feat * PSQT_BUCKETS;
        for (int b = 0; b < PSQT_BUCKETS; ++b)
            dst[b] = src[b] + add_row[b] - sub_row[b];
    }

    void Network::update_psqt_1a2s(int32_t *dst, const int32_t *src,
                                   int add_feat, int sub1_feat, int sub2_feat) const
    {
        const int16_t *add_row = psqt_weights_ + add_feat * PSQT_BUCKETS;
        const int16_t *sub1_row = psqt_weights_ + sub1_feat * PSQT_BUCKETS;
        const int16_t *sub2_row = psqt_weights_ + sub2_feat * PSQT_BUCKETS;
        for (int b = 0; b < PSQT_BUCKETS; ++b)
            dst[b] = src[b] + add_row[b] - sub1_row[b] - sub2_row[b];
    }

    // ============================================================================
    // Piece counting
    // ============================================================================

    int Network::count_pieces(const Board &board)
    {
        return popcount(board.pieces());
    }

    // ============================================================================
    // Forward pass -- accumulator -> SCReLU -> bucketed output -> centipawns
    // ============================================================================

    int Network::evaluate(const Accumulator &acc, int perspective, int bucket) const
    {
        int main_result;
#if defined(__AVX2__)
        main_result = forward_avx2(acc.values[0], acc.values[1], perspective, bucket);
#else
        main_result = forward_scalar(acc.values[0], acc.values[1], perspective, bucket);
#endif

        // PSQT contribution (v6): (friendly - enemy)[bucket] / QA -> centipawns
        int friendly_psqt = acc.psqt[perspective][bucket];
        int enemy_psqt = acc.psqt[1 - perspective][bucket];
        return main_result + (friendly_psqt - enemy_psqt) / QA;
    }

    // ============================================================================
    // Scalar implementations
    // ============================================================================

    void Network::add_column_scalar(int16_t *acc, const int16_t *col) const
    {
        for (int i = 0; i < FT_SIZE; ++i)
            acc[i] += col[i];
    }

    void Network::sub_column_scalar(int16_t *acc, const int16_t *col) const
    {
        for (int i = 0; i < FT_SIZE; ++i)
            acc[i] -= col[i];
    }

    void Network::apply_update_1a1s_scalar(int16_t *dst, const int16_t *src,
                                           int add_feat, int sub_feat) const
    {
        const int16_t *add_col = ft_weights_ + add_feat * FT_SIZE;
        const int16_t *sub_col = ft_weights_ + sub_feat * FT_SIZE;
        for (int i = 0; i < FT_SIZE; ++i)
            dst[i] = src[i] + add_col[i] - sub_col[i];
    }

    void Network::apply_update_1a2s_scalar(int16_t *dst, const int16_t *src,
                                           int add_feat, int sub1_feat, int sub2_feat) const
    {
        const int16_t *add_col = ft_weights_ + add_feat * FT_SIZE;
        const int16_t *sub1_col = ft_weights_ + sub1_feat * FT_SIZE;
        const int16_t *sub2_col = ft_weights_ + sub2_feat * FT_SIZE;
        for (int i = 0; i < FT_SIZE; ++i)
            dst[i] = src[i] + add_col[i] - sub1_col[i] - sub2_col[i];
    }

    int Network::forward_scalar(const int16_t *white_acc, const int16_t *black_acc,
                                int perspective, int bucket) const
    {
        const int16_t *friendly = (perspective == 0) ? white_acc : black_acc;
        const int16_t *enemy = (perspective == 0) ? black_acc : white_acc;

        const auto &bk = buckets_[bucket];

        // ── SCReLU on accumulator → build input for L1 ──
        // Clamp to [0, QA=255], square, then this becomes the "activation"
        // We'll compute L1 directly: for each L1 neuron, dot product with screlu outputs
        //
        // screlu(x) = clamp(x, 0, QA)² / QA  (keeps scale at QA)
        // So L1_input[i] = screlu(friendly[i]) for i < FT_SIZE
        //                 = screlu(enemy[i - FT_SIZE]) for i >= FT_SIZE

        // L1: int32 accumulation
        int32_t l1_out[L1_SIZE];
        for (int j = 0; j < L1_SIZE; ++j)
        {
            int32_t sum = bk.l1_bias[j];

            // Friendly half — use column-block layout: l1_weight_col[i/4][j*4 + i%4]
            for (int i = 0; i < FT_SIZE; ++i)
            {
                int16_t v = std::clamp(friendly[i], int16_t(SCRELU_MIN), int16_t(SCRELU_MAX));
                int32_t screlu_v = (int32_t(v) * int32_t(v)) / QA; // ∈ [0, QA]
                sum += screlu_v * int32_t(bk.l1_weight_col[i / 4][j * 4 + i % 4]);
            }
            // Enemy half
            for (int i = 0; i < FT_SIZE; ++i)
            {
                int16_t v = std::clamp(enemy[i], int16_t(SCRELU_MIN), int16_t(SCRELU_MAX));
                int32_t screlu_v = (int32_t(v) * int32_t(v)) / QA;
                sum += screlu_v * int32_t(bk.l1_weight_col[(FT_SIZE + i) / 4][j * 4 + (FT_SIZE + i) % 4]);
            }

            // Scale down and CReLU for L1 output
            // L1 bias is pre-scaled by QA*QB, weights by QB
            // After dot: sum is in units of QA*QB
            // CReLU: clamp to [0, QA*QB] (keeping scale)
            l1_out[j] = std::clamp(sum / (QA * QB), int32_t(0), int32_t(127));
        }

        // L2
        int32_t l2_out[L2_SIZE];
        for (int j = 0; j < L2_SIZE; ++j)
        {
            int32_t sum = bk.l2_bias[j];
            for (int i = 0; i < L1_SIZE; ++i)
                sum += l1_out[i] * int32_t(bk.l2_weight[j][i]);
            // De-scale and CReLU
            l2_out[j] = std::clamp(sum / QB, int32_t(0), int32_t(127));
        }

        // L3 + skip13 (v6): add residual from L1 to L3 pre-activation
        int32_t l3_out[L3_SIZE];
        for (int j = 0; j < L3_SIZE; ++j)
        {
            int32_t sum = bk.l3_bias[j];
            for (int i = 0; i < L2_SIZE; ++i)
                sum += l2_out[i] * int32_t(bk.l3_weight[j][i]);
            // skip13: L1 output contributes to L3 pre-activation
            for (int i = 0; i < L1_SIZE; ++i)
                sum += l1_out[i] * int32_t(bk.skip13_weight[j][i]);
            l3_out[j] = std::clamp(sum / QB, int32_t(0), int32_t(127));
        }

        // Output
        int32_t result = bk.out_bias;
        for (int i = 0; i < L3_SIZE; ++i)
            result += l3_out[i] * int32_t(bk.out_weight[i]);

        // Final de-scale to centipawns
        // The quantisation chain: QA (FT) * QB (L1) * QB (L2) * QB (L3) * QB (out)
        // We already divided by QA*QB in L1, by QB in L2, by QB in L3, so remaining is QB
        return int(result / QB);
    }

} // namespace Chess::NNUE
