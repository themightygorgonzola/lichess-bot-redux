// ============================================================================
// NNUE Eval — public evaluation interface implementation
// ============================================================================

#include "nnue_eval.h"
#include "nnue_network.h"
#include "nnue_updater.h"
#include "nnue_accumulator.h"
#include "../board.h"
#include "../eval.h"      // HCE fallback
#include <iostream>
#include <atomic>
#include <sstream>
#include <filesystem>
#ifdef PROFILE
    #ifdef _MSC_VER
        #include <intrin.h>
    #else
        #include <x86intrin.h>
    #endif
#endif

namespace Chess::NNUE {

// ============================================================================
// Global state
// ============================================================================

static bool s_use_nnue   = false;
static bool s_nnue_enabled = true;   // runtime toggle — setoption name UseNNUE
static bool s_info_enabled = true;
static bool s_profile_enabled = true;
static std::string s_loaded_path;  // track which file is loaded to avoid reloads

// Debug counters — printed via UCI info string
static std::atomic<uint64_t> s_eval_count{0};
static std::atomic<uint64_t> s_refresh_count{0};
static std::atomic<uint64_t> s_fallback_count{0};
static std::atomic<uint64_t> s_forward_count{0};
static std::atomic<uint64_t> s_incremental_update_count{0};
static std::atomic<uint64_t> s_capture_update_count{0};
static std::atomic<uint64_t> s_full_update_count{0};
static std::atomic<uint64_t> s_null_update_count{0};
static std::atomic<uint64_t> s_cycles_refresh{0};
static std::atomic<uint64_t> s_cycles_forward{0};
static std::atomic<uint64_t> s_cycles_update{0};
static std::atomic<uint64_t> s_cycles_full_update{0};
static std::atomic<uint64_t> s_cycles_null_update{0};

#ifdef PROFILE
static inline uint64_t profile_cycles_now() {
    return __rdtsc();
}
#else
static inline uint64_t profile_cycles_now() {
    return 0;
}
#endif

// ============================================================================
// nnue_init — load weights from disk
// ============================================================================

bool nnue_init(const std::string& path) {
    // Skip if we already loaded from this exact path
    if (s_use_nnue && path == s_loaded_path)
        return true;

    // Try the path as given; if that fails, try resolving it relative to the
    // already-discovered exe directory (covers the case where UCI sends a
    // relative path like "bot/engine/nn.bin" from a different cwd).
    namespace fs = std::filesystem;
    std::string resolved = path;
    if (!fs::exists(resolved) && !s_loaded_path.empty()) {
        // Try relative to the directory of the previously loaded file
        try {
            fs::path attempt = fs::path(s_loaded_path).parent_path() / fs::path(path).filename();
            if (fs::exists(attempt))
                resolved = attempt.string();
        } catch (...) {}
    }

    if (g_network.load(resolved)) {
        s_use_nnue = true;
        s_loaded_path = resolved;
        if (s_info_enabled)
            std::cout << "info string NNUE loaded from " << resolved << std::endl;
        return true;
    }

    // Don't clobber a successfully-loaded network if the new path just failed
    if (s_use_nnue) {
        if (s_info_enabled)
            std::cout << "info string NNUE: could not load '" << path
                      << "', keeping existing network" << std::endl;
    } else {
        if (s_info_enabled)
            std::cout << "info string NNUE load failed, using HCE" << std::endl;
    }
    return false;
}

bool nnue_auto_discover(const char* argv0) {
    if (s_use_nnue)
        return true;
    // Try to find nn.bin near the engine binary:
    //   1. Same directory as the executable
    //   2. Parent directory (e.g. bot/ when exe is in bot/engine/)
    //   3. Two levels up (project root)
    namespace fs = std::filesystem;
    try {
        fs::path exe = fs::canonical(fs::path(argv0));
        fs::path dir = exe.parent_path();
        for (int i = 0; i < 3; ++i) {
            fs::path candidate = dir / "nn.bin";
            if (fs::exists(candidate)) {
                return nnue_init(candidate.string());
            }
            if (dir.has_parent_path() && dir.parent_path() != dir)
                dir = dir.parent_path();
            else
                break;
        }
    } catch (...) {
        // If filesystem ops fail, just continue without NNUE
    }
    if (s_info_enabled)
        std::cout << "info string No nn.bin found, using HCE" << std::endl;
    return false;
}

bool nnue_is_loaded() {
    return s_use_nnue;
}

void nnue_set_enabled(bool enabled) {
    s_nnue_enabled = enabled;
    if (s_info_enabled)
        std::cout << "info string UseNNUE set to "
                  << (enabled ? "true" : "false") << std::endl;
}

bool nnue_is_enabled() {
    return s_nnue_enabled;
}

void nnue_set_info_enabled(bool enabled) {
    s_info_enabled = enabled;
}

bool nnue_info_enabled() {
    return s_info_enabled;
}

void nnue_set_profile_enabled(bool enabled) {
    s_profile_enabled = enabled;
}

bool nnue_profile_enabled() {
    return s_profile_enabled;
}

// ============================================================================
// nnue_evaluate — main entry point
//
// 1. If the NNUE net is not loaded → fall back to HCE.
// 2. If the current accumulator is not computed → full refresh it.
//    (The incremental path should keep it valid, but this is a safety net.)
// 3. Run the forward pass and return centipawns from STM's perspective.
// ============================================================================

int nnue_evaluate(const Board& board) {
    if (!s_use_nnue || !s_nnue_enabled) {
        if (s_profile_enabled)
            s_fallback_count.fetch_add(1, std::memory_order_relaxed);
        return evaluate(board);
    }

    Accumulator& acc = board.nnue_accumulator();

    if (s_profile_enabled)
        s_eval_count.fetch_add(1, std::memory_order_relaxed);

    // Safety: ensure the accumulator is valid
    if (!acc.computed) {
        if (s_profile_enabled)
            s_refresh_count.fetch_add(1, std::memory_order_relaxed);
        uint64_t t0 = s_profile_enabled ? profile_cycles_now() : 0;
        g_network.refresh(acc, board);
        if (s_profile_enabled)
            s_cycles_refresh.fetch_add(profile_cycles_now() - t0, std::memory_order_relaxed);
    }

    // perspective: 0 for white-to-move, 1 for black-to-move
    int perspective = int(board.side_to_move());

    // Compute output bucket from piece count
    int pc = Network::count_pieces(board);
    int bucket = piece_count_bucket(pc);

    if (s_profile_enabled)
        s_forward_count.fetch_add(1, std::memory_order_relaxed);
    uint64_t t1 = s_profile_enabled ? profile_cycles_now() : 0;
    int score = g_network.evaluate(acc, perspective, bucket);
    if (s_profile_enabled)
        s_cycles_forward.fetch_add(profile_cycles_now() - t1, std::memory_order_relaxed);
    return score;
}

std::string nnue_refresh_stats() {
    uint64_t ev  = s_eval_count.load(std::memory_order_relaxed);
    uint64_t ref = s_refresh_count.load(std::memory_order_relaxed);
    int pct = ev > 0 ? int(100 * ref / ev) : 0;
    std::ostringstream ss;
    ss << "evals=" << ev << " refreshes=" << ref << " pct=" << pct << "%";
    return ss.str();
}

void nnue_profile_reset() {
    if (!s_profile_enabled)
        return;
    s_eval_count.store(0, std::memory_order_relaxed);
    s_refresh_count.store(0, std::memory_order_relaxed);
    s_fallback_count.store(0, std::memory_order_relaxed);
    s_forward_count.store(0, std::memory_order_relaxed);
    s_incremental_update_count.store(0, std::memory_order_relaxed);
    s_capture_update_count.store(0, std::memory_order_relaxed);
    s_full_update_count.store(0, std::memory_order_relaxed);
    s_null_update_count.store(0, std::memory_order_relaxed);
    s_cycles_refresh.store(0, std::memory_order_relaxed);
    s_cycles_forward.store(0, std::memory_order_relaxed);
    s_cycles_update.store(0, std::memory_order_relaxed);
    s_cycles_full_update.store(0, std::memory_order_relaxed);
    s_cycles_null_update.store(0, std::memory_order_relaxed);
}

ProfileSnapshot nnue_profile_snapshot() {
    ProfileSnapshot snap;
    if (!s_profile_enabled)
        return snap;
#ifdef PROFILE
    snap.cycle_counters_enabled = true;
#endif
    snap.loaded = s_use_nnue;
    snap.runtime_enabled = s_nnue_enabled;
    snap.eval_calls = s_eval_count.load(std::memory_order_relaxed);
    snap.fallback_calls = s_fallback_count.load(std::memory_order_relaxed);
    snap.refresh_calls = s_refresh_count.load(std::memory_order_relaxed);
    snap.forward_calls = s_forward_count.load(std::memory_order_relaxed);
    snap.incremental_updates = s_incremental_update_count.load(std::memory_order_relaxed);
    snap.capture_updates = s_capture_update_count.load(std::memory_order_relaxed);
    snap.full_updates = s_full_update_count.load(std::memory_order_relaxed);
    snap.null_updates = s_null_update_count.load(std::memory_order_relaxed);
    snap.cycles_refresh = s_cycles_refresh.load(std::memory_order_relaxed);
    snap.cycles_forward = s_cycles_forward.load(std::memory_order_relaxed);
    snap.cycles_update = s_cycles_update.load(std::memory_order_relaxed);
    snap.cycles_full_update = s_cycles_full_update.load(std::memory_order_relaxed);
    snap.cycles_null_update = s_cycles_null_update.load(std::memory_order_relaxed);
    return snap;
}

void nnue_profile_note_do_move_update(bool full_refresh, bool capture, uint64_t cycles) {
    if (!s_profile_enabled)
        return;
    if (full_refresh) {
        s_full_update_count.fetch_add(1, std::memory_order_relaxed);
        s_cycles_full_update.fetch_add(cycles, std::memory_order_relaxed);
    } else {
        s_incremental_update_count.fetch_add(1, std::memory_order_relaxed);
        if (capture) s_capture_update_count.fetch_add(1, std::memory_order_relaxed);
        s_cycles_update.fetch_add(cycles, std::memory_order_relaxed);
    }
}

void nnue_profile_note_null_update(uint64_t cycles) {
    if (!s_profile_enabled)
        return;
    s_null_update_count.fetch_add(1, std::memory_order_relaxed);
    s_cycles_null_update.fetch_add(cycles, std::memory_order_relaxed);
}

void nnue_profile_note_full_update(uint64_t cycles) {
    if (!s_profile_enabled)
        return;
    s_full_update_count.fetch_add(1, std::memory_order_relaxed);
    s_cycles_full_update.fetch_add(cycles, std::memory_order_relaxed);
}

} // namespace Chess::NNUE
