#pragma once
// ============================================================================
// NNUE Accumulator — per-position hidden-layer state (v2)
//
// Holds the FT_SIZE-dimensional feature transformer activations for each
// perspective (white, black). Stored in the engine's StateInfo stack so
// do_move / undo_move maintain it incrementally.
//
// Full refresh: one 40960×1024 sparse matmul (~30 column adds).
// Incremental:  1–4 column add/sub ops (each 1024 int16 adds).
// ============================================================================

#include "nnue_arch.h"
#include <cstdint>
#include <cstring>

namespace Chess::NNUE
{

    struct Accumulator
    {
        // FT pre-activation values (before SCReLU).
        // Indexed [perspective][neuron].   perspective: 0=white, 1=black
        alignas(64) int16_t values[2][FT_SIZE];

        // PSQT accumulator (v6): one int32 per (perspective, bucket).
        // Accumulates alongside the FT with no extra latency for incremental updates.
        // Friendly - enemy contribution is added to the final centipawn output.
        int32_t psqt[2][PSQT_BUCKETS];

        // Has this accumulator been computed yet?
        bool computed = false;

        void clear()
        {
            std::memset(values, 0, sizeof(values));
            std::memset(psqt, 0, sizeof(psqt));
            computed = false;
        }
    };

} // namespace Chess::NNUE
