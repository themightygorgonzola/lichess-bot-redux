#pragma once

#include "board.h"
#include "move.h"
#include "movegen.h"
#include "nnue/nnue_eval.h"
#include "tt.h"
#include <thread>
#include <atomic>
#include <vector>
#include <memory>
#include <mutex>
#include <condition_variable>
#include <functional>
#include <chrono>
#ifdef PROFILE
#ifdef _MSC_VER
#include <intrin.h>
#else
#include <x86intrin.h>
#endif
#endif

// ============================================================================
// Search engine.
//
// Current behavior:
//   • All searches (1T and MT) use the same alpha-beta with aspiration windows.
//   • Multithreaded search uses Lazy SMP: each thread independently runs the
//     full iterative-deepening loop, sharing only the transposition table and
//     the atomic stop flag.  Depth staggering provides search diversity.
// ============================================================================

namespace Chess
{

    // Which parallel search strategy to use (retained for compatibility)
    enum SearchMode
    {
        LAZY_SMP,
        ROOT_SPLIT
    };

    // Search limits (from UCI "go" command)
    struct SearchLimits
    {
        int depth = MAX_PLY;   // Max depth
        int64_t nodes = 0;     // Max nodes (0 = unlimited)
        int movetime = 0;      // Exact milliseconds to search
        int wtime = 0;         // White clock (ms)
        int btime = 0;         // Black clock (ms)
        int winc = 0;          // White increment (ms)
        int binc = 0;          // Black increment (ms)
        int movestogo = 0;     // Moves until next time control
        int opp_time = 0;      // Opponent's remaining time (ms), for TM tuning
        bool infinite = false; // Search until "stop"
        bool ponder = false;   // Ponder mode
    };

    // Search statistics
    struct SearchStats
    {
        int64_t nodes = 0;
        int depth = 0;
        int seldepth = 0;
        int score = VALUE_NONE;
        Move bestmove = Move::none();
        Move ponder_move = Move::none();
        std::chrono::steady_clock::time_point start_time;

        int64_t elapsed_ms() const
        {
            auto now = std::chrono::steady_clock::now();
            return std::chrono::duration_cast<std::chrono::milliseconds>(now - start_time).count();
        }
    };

    struct SearchDiagCounters
    {
        int64_t alpha_beta_nodes = 0;
        int64_t quiescence_nodes = 0;
        int64_t tt_probes = 0;
        int64_t tt_hits = 0;
        int64_t tt_cutoffs = 0;
        int64_t null_move_cuts = 0;
        int64_t rfp_cuts = 0;
        int64_t futility_prunes = 0;
        int64_t lmp_prunes = 0;
        int64_t see_prunes = 0;
        int64_t hist_prunes = 0;
        int64_t lmr_searches = 0;
        int64_t lmr_re_searches = 0;
        int64_t razoring_cuts = 0;
        int64_t probcut_cuts = 0;
        int64_t tb_hits = 0;

        void clear() { std::memset(this, 0, sizeof(*this)); }

        SearchDiagCounters &operator+=(const SearchDiagCounters &other)
        {
            alpha_beta_nodes += other.alpha_beta_nodes;
            quiescence_nodes += other.quiescence_nodes;
            tt_probes += other.tt_probes;
            tt_hits += other.tt_hits;
            tt_cutoffs += other.tt_cutoffs;
            null_move_cuts += other.null_move_cuts;
            rfp_cuts += other.rfp_cuts;
            futility_prunes += other.futility_prunes;
            lmp_prunes += other.lmp_prunes;
            see_prunes += other.see_prunes;
            hist_prunes += other.hist_prunes;
            lmr_searches += other.lmr_searches;
            lmr_re_searches += other.lmr_re_searches;
            razoring_cuts += other.razoring_cuts;
            probcut_cuts += other.probcut_cuts;
            tb_hits += other.tb_hits;
            return *this;
        }
    };

    struct SearchProfile
    {
        bool cycle_counters_enabled = false;
        int threads = 0;
        int64_t nodes = 0;
        uint64_t cycles_total = 0;
        uint64_t cycles_nnue = 0;
        uint64_t cycles_movegen = 0;
        uint64_t cycles_do_move = 0;
        uint64_t cycles_undo_move = 0;
        uint64_t cycles_see = 0;
        uint64_t cycles_gcheck = 0;
        SearchDiagCounters diag;
        NNUE::ProfileSnapshot nnue;
    };

    struct SearchSmpStats
    {
        int threads = 0;
    };

    // ============================================================================
    // SearchParams — all tunable search constants in one configurable struct.
    // Defaults match the current hand-tuned values. Modify via UCI setoption or
    // pass programmatically before searching. Call rebuild_lmr() after changing
    // lmr_base or lmr_divisor.
    // ============================================================================
    struct SearchParams
    {
        // ── LMR (Late Move Reductions) ─────────────────────────────────────────
        double lmr_base = 0.75;    // b91: sf_guided_tune (was 0.70)
        double lmr_divisor = 1.80; // b91: sf_guided_tune (was 1.55)
        int lmr_min_depth = 3;     // minimum depth to apply LMR
        int lmr_min_moves = 2;     // minimum moves searched before LMR kicks in
        int lmr_pv_sub = 1;        // reduce less in PV nodes
        int lmr_improving_add = 1; // extra reduction when NOT improving
        int lmr_cutnode_add = 2;   // extra reduction at expected cut-nodes
        int lmr_hist_div = 8000;   // b91: sf_guided_tune (was 11000)
        int lmr_hist_bad_add = 1;  // extra reduction when stat_score < hist_bad_thresh
        int lmr_hist_bad_thresh = -4000;
        bool lmr_check_sub = true; // reduce less if move gives check

        // ── RFP (Reverse Futility Pruning) ─────────────────────────────────────
        int rfp_max_depth = 7;
        int rfp_margin = 60;        // b92: sf_guided_tune v2 (was 50)
        int rfp_improving_sub = 20; // b91: sf_guided_tune (was 40)

        // ── Null Move Pruning ──────────────────────────────────────────────────
        int nmp_min_depth = 3;
        int nmp_base_r = 4;     // tuned (was 3): base reduction R
        int nmp_depth_div = 4;  // b90: reverted 3→4 (node-count tune hurt eval-heavy positions)
        int nmp_eval_div = 250; // b89: 200→250 (stable across 3 iters)
        int nmp_max_bonus = 3;

        // ── Futility Pruning ───────────────────────────────────────────────────
        int fp_max_depth = 7;
        int fp_base = 100;       // b91: sf_guided_tune (was 70, node-count tune was wrong)
        int fp_depth_scale = 60; // b89: 65→60 (+2.9% pruning)
        int fp_improving_sub = 50;

        // ── LMP (Late Move Pruning) ────────────────────────────────────────────
        int lmp_max_depth = 7;
        int lmp_improving_base = 5; // threshold when improving
        int lmp_base = 3;           // threshold when not improving

        // ── SEE Pruning ────────────────────────────────────────────────────────
        int see_quiet_scale = 20; // quiet: threshold = -scale * depth^2
        int see_capt_scale = 90;  // b93: reverted 160→90 (b80 value); sf_guided_tune v2 over-pruned speculative captures — probe filtered out the positions where SEE matters most
        int see_capt_hist_div = 32;
        int see_capt_hist_max = 138; // clamp lower bound multiplier
        int see_capt_hist_min = 135; // clamp upper bound multiplier

        // ── History-based Quiet Pruning ────────────────────────────────────────
        int hist_prune_max_depth = 8;
        int hist_prune_scale = 2500; // tuned (was 3000): threshold = -scale * depth

        // ── Aspiration Windows ─────────────────────────────────────────────────
        int asp_delta = 50;
        int asp_min_depth = 5;
        int asp_max_delta = 900;

        // ── IIR (Internal Iterative Reduction) ────────────────────────────────
        int iir_min_depth = 4;

        // ── Singular Extension ─────────────────────────────────────────────────
        int se_min_depth = 8;
        int se_tt_depth_margin = 3; // TT entry depth must be >= depth - margin
        int se_beta_scale = 3;      // singular_beta = tt_score - scale * depth

        // ── ProbCut ────────────────────────────────────────────────────────────
        int probcut_min_depth = 5;
        int probcut_beta_margin = 100;

        // ── Razoring ──────────────────────────────────────────────────────────
        int razor_max_depth = 2;
        int razor_base = 300;
        int razor_depth_scale = 250;
    };

    // Global search params — modified by UCI setoption or tune harness.
    extern SearchParams SP;

    // Per-thread search data
    struct ThreadData
    {
        Board board; // Thread-local copy of the board
        int thread_id = 0;
        int64_t nodes = 0;
        int seldepth = 0;
        int root_depth = 0; // Current iteration's root depth (for extension cap)

        ThreadData()
        {
            clear();
        }

        // Killer moves for move ordering (indexed by ply)
        Move killers[MAX_PLY][2];

        // History heuristic: [color][from][to]
        int history[COLOR_NB][SQUARE_NB][SQUARE_NB];

        // Countermove table: [previous_piece][previous_to] -> refutation move
        Move countermove[PIECE_NB][SQUARE_NB];

        // State stack for do_move
        StateInfo states[MAX_PLY + 10];

        // Static eval at each ply (for "improving" detection)
        int eval_stack[MAX_PLY + 10];

        // Excluded move for singular extension (indexed by ply)
        Move excluded[MAX_PLY];

        // Continuation history: [prev_piece_type][prev_to][cur_piece_type][cur_to]
        // Indexed by PieceType (7 values) instead of Piece (16) — keeps the table
        // at ~785KB so it fits in L2 cache (was 16MB with Piece indexing).
        int conthist[PIECE_TYPE_NB][SQUARE_NB][PIECE_TYPE_NB][SQUARE_NB];

        // 2-ply continuation history: [piece_type_2_plies_ago][to_2_plies_ago][cur_piece_type][cur_to]
        // Captures the "what did I do 2 plies ago?" signal for move ordering.
        int conthist2[PIECE_TYPE_NB][SQUARE_NB][PIECE_TYPE_NB][SQUARE_NB];

        // Capture history: [moving_piece][to_square][captured_piece_type]
        // Better ordering for captures than pure MVV-LVA
        int capture_history[PIECE_NB][SQUARE_NB][PIECE_TYPE_NB];

        // Correction history: pawn-structure-keyed eval error correction
        // Records (search_score - raw_eval) to adjust static eval at similar pawn structures
        static constexpr int CORR_HIST_SIZE = 16384;
        static constexpr int CORR_GRAIN = 256;
        static constexpr int CORR_LIMIT = 1024;
        static constexpr int CORR_MAX = 256 * 64; // ±64cp max correction
        int pawn_correction[CORR_HIST_SIZE];

        // ── Per-thread eval cache ─────────────────────────────────────────────
        // Direct-mapped hash from Zobrist key → raw static eval (cp).
        // Calling evaluate() on a position multiple times via different
        // transposition orders is extremely common in alpha-beta — caching
        // each raw eval cuts redundant eval cost dramatically (HCE eval is
        // ~960 lines and called per interior + per quiescence node).
        //
        // 65536 entries � 16 bytes = 1 MB per thread (b59: doubled from 32K, +5.5% NPS on 9950X3D). Direct-mapped (no
        // bucket loop, latest-writer-wins on collision). Key is compared on
        // probe so collisions never produce wrong scores.
        static constexpr int EVAL_CACHE_SIZE = 1 << 16; // 65536
        static constexpr int EVAL_CACHE_MASK = EVAL_CACHE_SIZE - 1;
        struct EvalCacheEntry
        {
            Key key = 0;
            int32_t score = 0;
        };
        EvalCacheEntry eval_cache[EVAL_CACHE_SIZE];

        // Per-ply info for multi-ply continuation history lookback
        struct PlyInfo
        {
            Move current_move = Move::none();
            Piece moved_piece = NO_PIECE; // Piece on destination after move
        };
        PlyInfo ply_info[MAX_PLY + 10];

#ifdef PROFILE
        // Cycle counters for profiling — zeroed in soft_reset()
        uint64_t cycles_nnue = 0;
        uint64_t cycles_movegen = 0;
        uint64_t cycles_do_move = 0;
        uint64_t cycles_undo_move = 0;
        uint64_t cycles_islegal = 0;
        uint64_t cycles_see = 0;
        uint64_t cycles_gcheck = 0;
#endif

        // Diagnostic search counters — always active, nearly zero overhead.
        // Zeroed per depth iteration in the iterative-deepening loop.
        SearchDiagCounters diag;

        // Full clear: reset everything (call on ucinewgame)
        void clear()
        {
            nodes = 0;
            seldepth = 0;
            root_depth = 0;
            std::memset(killers, 0, sizeof(killers));
            std::memset(history, 0, sizeof(history));
            std::memset(countermove, 0, sizeof(countermove));
            std::memset(eval_stack, 0, sizeof(eval_stack));
            for (int i = 0; i < MAX_PLY; ++i)
                excluded[i] = Move::none();
            std::memset(conthist, 0, sizeof(conthist));
            std::memset(conthist2, 0, sizeof(conthist2));
            std::memset(capture_history, 0, sizeof(capture_history));
            std::memset(pawn_correction, 0, sizeof(pawn_correction));
            std::memset(eval_cache, 0, sizeof(eval_cache));
            for (int i = 0; i < MAX_PLY + 10; ++i)
                ply_info[i] = PlyInfo{};
            diag.clear();
        }

        // Soft reset: preserve learned history tables, only clear per-search state.
        // Called between "go" commands so history knowledge carries across moves.
        void soft_reset()
        {
            nodes = 0;
            seldepth = 0;
            root_depth = 0;
            std::memset(killers, 0, sizeof(killers));
            std::memset(eval_stack, 0, sizeof(eval_stack));
            for (int i = 0; i < MAX_PLY; ++i)
                excluded[i] = Move::none();
            for (int i = 0; i < MAX_PLY + 10; ++i)
                ply_info[i] = PlyInfo{};
            diag.clear();
#ifdef PROFILE
            cycles_nnue = cycles_movegen = cycles_do_move = cycles_undo_move = cycles_islegal = cycles_see = cycles_gcheck = 0;
#endif
        }

        // Age history scores (decay by half) — call between searches
        void age_history()
        {
            for (int c = 0; c < COLOR_NB; ++c)
                for (int f = 0; f < SQUARE_NB; ++f)
                    for (int t = 0; t < SQUARE_NB; ++t)
                        history[c][f][t] /= 2;
            // Age continuation history (1-ply)
            int *ch = &conthist[0][0][0][0];
            constexpr int CH_SIZE = int(PIECE_TYPE_NB) * int(SQUARE_NB) * int(PIECE_TYPE_NB) * int(SQUARE_NB);
            for (int i = 0; i < CH_SIZE; ++i)
                ch[i] /= 2;
            // Age continuation history (2-ply)
            int *ch2 = &conthist2[0][0][0][0];
            for (int i = 0; i < CH_SIZE; ++i)
                ch2[i] /= 2;
            // Age capture history
            int *caph = &capture_history[0][0][0];
            constexpr int CAPH_SIZE = int(PIECE_NB) * int(SQUARE_NB) * int(PIECE_TYPE_NB);
            for (int i = 0; i < CAPH_SIZE; ++i)
                caph[i] /= 2;
            // Age correction history
            for (int i = 0; i < CORR_HIST_SIZE; ++i)
                pawn_correction[i] /= 2;
        }
    };

    class SearchEngine
    {
    public:
        SearchEngine();
        ~SearchEngine();

        // Set number of threads
        void set_threads(int n);

        // Full reset of all thread-local learned data (call on ucinewgame)
        void clear_all()
        {
            for (auto &td : thread_data_)
                td->clear();
        }

        // Set search strategy
        void set_mode(SearchMode m) { mode_ = m; }
        SearchMode mode() const { return mode_; }

        // Start a search (blocking call on main thread)
        Move search(Board &board, const SearchLimits &limits);

        // Stop the current search
        void stop();

        // Ponderhit: transition a ponder search into a timed search
        void ponderhit(int wtime, int btime, int winc, int binc, int movestogo);

        // Is search currently running?
        bool is_searching() const { return searching_.load(); }
        bool is_pondering() const { return pondering_.load(); }

        // Get latest stats (thread-safe snapshot for API polling)
        SearchStats get_stats() const { return main_stats_; }
        SearchProfile last_profile() const { return last_profile_; }
        SearchSmpStats last_smp() const { return last_smp_; }
        void set_profile_enabled(bool enabled) { profile_enabled_.store(enabled, std::memory_order_relaxed); }
        bool profile_enabled() const { return profile_enabled_.load(std::memory_order_relaxed); }
        void set_tt_enabled(bool enabled) { tt_enabled_ = enabled; }

        // Control whether search prints UCI-style info lines to stdout.
        void set_info_enabled(bool enabled) { info_enabled_ = enabled; }
        bool info_enabled() const { return info_enabled_; }

        // Control expensive per-depth diagnostics (childeval, rootmoves, searchdiag,
        // moveorder). Kept separate from info_enabled so bench always stays clean.
        void set_diag_enabled(bool enabled) { diag_enabled_ = enabled; }
        bool diag_enabled() const { return diag_enabled_; }

        // Re-build the LMR table from the current SP values.
        // Call after changing SP.lmr_base or SP.lmr_divisor.
        void rebuild_lmr();

        // Confidence threshold tuning (0=fast/aggressive, 100=deep/thorough)
        void set_thoughtfulness(int t) { thoughtfulness_ = std::clamp(t, 0, 100); }
        int thoughtfulness() const { return thoughtfulness_; }

        // UCI info callback
        using InfoCallback = std::function<void(const SearchStats &, const std::string &pv)>;
        void set_info_callback(InfoCallback cb) { info_callback_ = cb; }

    private:
        // Alpha-beta search
        int alpha_beta(ThreadData &td, int depth, int alpha, int beta, int ply, bool is_pv, bool cutNode = false, Move prev_move = Move::none());

        // Quiescence search
        int quiescence(ThreadData &td, int alpha, int beta, int ply);

        // Time management
        int allocate_time(const SearchLimits &limits, Color side);
        bool should_stop(const ThreadData &td) const;

        // Move ordering
        void score_moves(ThreadData &td, MoveList &moves, Move tt_move, int ply, Move prev_move = Move::none());

        // Lazy SMP worker (runs independent ID loop on helper threads)
        void worker_lazy_smp(int thread_id, Board board, const SearchLimits &limits);

        // Persistent thread pool worker (sleeps between searches)
        void worker_thread_loop(int thread_id);

        // Root-split: search a subset of root moves for one depth iteration
        void root_split_search(ThreadData &td, const Board &board,
                               const MoveList &root_moves, int depth,
                               int thread_id, int num_workers);

        // Shut down persistent thread pool
        void shutdown_pool();

        // Build and print UCI PV from TT
        void print_info(const SearchStats &stats, const std::string &pv);
        void finalize_profile(int64_t total_nodes);
        void dump_profile_stderr() const;

        // Members
        SearchMode mode_ = LAZY_SMP;
        int num_threads_ = 1;
        mutable std::atomic<bool> stop_flag_{false};
        std::atomic<bool> searching_{false};
        std::atomic<bool> pondering_{false}; // true when in ponder mode (no time limit)
        std::atomic<bool> profile_enabled_{true};
        std::atomic<bool> *external_stop_ = nullptr;
        Color ponder_side_ = WHITE; // side to move during ponder position
        SearchLimits saved_limits_; // saved from 'go' for ponderhit fallback
        int64_t max_time_ms_ = 0;
        int64_t soft_time_ms_ = 0; // Soft limit: stop after completing current depth
        int64_t hard_time_ms_ = 0; // Hard limit: stop immediately
        int thoughtfulness_ = 70;  // TM confidence threshold (0-100)

        SearchStats main_stats_;
        SearchProfile last_profile_;
        SearchSmpStats last_smp_;
        std::vector<std::unique_ptr<ThreadData>> thread_data_;
        std::vector<std::thread> threads_;      // lazy SMP per-search threads
        std::vector<std::thread> pool_threads_; // persistent pool threads (ROOT_SPLIT)
        bool info_enabled_ = true;
        bool diag_enabled_ = false;
        bool tt_enabled_ = true;

        InfoCallback info_callback_;

        // --- Persistent thread pool (used by ROOT_SPLIT mode) ---
        std::mutex pool_mutex_;
        std::condition_variable pool_cv_;      // wake helper threads
        std::condition_variable pool_done_cv_; // helpers signal completion
        bool pool_active_ = false;             // threads are alive (false = shut down)
        bool pool_go_ = false;                 // signal: start a depth iteration
        int pool_done_count_ = 0;              // how many helpers finished this iteration

        // --- Root-split shared state (valid only during a depth iteration) ---
        Board rs_board_;                                  // root position for current search
        SearchLimits rs_limits_;                          // limits for current search
        MoveList rs_root_moves_;                          // sorted root moves for current depth
        int rs_depth_ = 0;                                // current ID depth being searched
        std::atomic<int> rs_best_score_{-VALUE_INFINITE}; // shared best score (for alpha)
        Move rs_best_move_ = Move::none();
        std::mutex rs_result_mutex_; // guards rs_best_move_ updates
    };

    // Global search instance
    extern SearchEngine Search;

} // namespace Chess
