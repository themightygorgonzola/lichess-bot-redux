#include "search.h"
#include "eval.h"
#include "nnue/nnue_eval.h"
#include "movegen.h"
#include "syzygy/syzygy.h"
#include <algorithm>
#include <climits>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <vector>
#include <cmath>

// ============================================================================
// Pre-computed LMR reduction table [depth][moveCount]
// ============================================================================
static int lmr_table[128][256];

// Global search params (defaults match previous hardcoded values).
// Modify via UCI setoption or tune harness; call init_lmr() after changing lmr_*.
Chess::SearchParams Chess::SP;

static void init_lmr()
{
    for (int d = 0; d < 128; d++)
        for (int m = 0; m < 256; m++)
            lmr_table[d][m] = (d == 0 || m == 0) ? 0
                                                 : int(Chess::SP.lmr_base + std::log(d) * std::log(m) / Chess::SP.lmr_divisor);
}

// History update with gravity â€” naturally bounds values, avoids overflow
static inline void update_history(int &entry, int bonus)
{
    entry += bonus - entry * std::abs(bonus) / 16384;
}

// History bonus magnitude: SF-style scaling (much larger than depth*depth)
static inline int history_bonus(int depth)
{
    return std::min(16 * depth + 1024, 1936);
}

namespace Chess
{

    namespace
    {
        static inline bool runtime_profile_enabled()
        {
            return Search.profile_enabled();
        }

        static bool search_trace_enabled()
        {
            static const bool enabled = []()
            {
                const char *v = std::getenv("REDUX_TRACE_SEARCH");
                if (!v || !*v)
                    return false;
                std::string s(v);
                std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c)
                               { return static_cast<char>(std::tolower(c)); });
                return s != "0" && s != "false" && s != "off";
            }();
            return enabled;
        }

        static void trace_search(const std::string &msg)
        {
            if (search_trace_enabled())
            {
                std::cerr << "search-trace " << msg << "\n";
            }
        }
    }

// ============================================================================
// Inline call-site wrappers.
// Under PROFILE builds, each wrapper accumulates rdtsc cycles into
// the corresponding ThreadData::cycles_* counter.
// Production builds: zero-overhead direct calls.
// ============================================================================
#ifdef PROFILE

    static inline int timed_nnue(ThreadData &td)
    {
        Key k = td.board.key();
        auto &slot = td.eval_cache[k & ThreadData::EVAL_CACHE_MASK];
        if (slot.key == k)
            return slot.score;
        uint64_t t0 = runtime_profile_enabled() ? __rdtsc() : 0;
        int r = NNUE::nnue_evaluate(td.board);
        if (runtime_profile_enabled())
            td.cycles_nnue += __rdtsc() - t0;
        slot.key = k;
        slot.score = r;
        return r;
    }
    static inline void timed_movegen(ThreadData &td, MoveList &moves, GenType gt = ALL_MOVES)
    {
        uint64_t t0 = runtime_profile_enabled() ? __rdtsc() : 0;
        generate_legal(td.board, moves, gt);
        if (runtime_profile_enabled())
            td.cycles_movegen += __rdtsc() - t0;
    }
    static inline bool timed_see(ThreadData &td, Move m, int threshold)
    {
        uint64_t t0 = runtime_profile_enabled() ? __rdtsc() : 0;
        bool r = td.board.see_ge(m, threshold);
        if (runtime_profile_enabled())
            td.cycles_see += __rdtsc() - t0;
        return r;
    }
    static inline bool timed_gcheck(ThreadData &td, Move m)
    {
        uint64_t t0 = runtime_profile_enabled() ? __rdtsc() : 0;
        bool r = td.board.gives_check(m);
        if (runtime_profile_enabled())
            td.cycles_gcheck += __rdtsc() - t0;
        return r;
    }
    static inline void timed_do_move(ThreadData &td, Move m, StateInfo &st)
    {
        uint64_t t0 = runtime_profile_enabled() ? __rdtsc() : 0;
        td.board.do_move(m, st);
        if (runtime_profile_enabled())
            td.cycles_do_move += __rdtsc() - t0;
    }
    static inline void timed_undo_move(ThreadData &td, Move m)
    {
        uint64_t t0 = runtime_profile_enabled() ? __rdtsc() : 0;
        td.board.undo_move(m);
        if (runtime_profile_enabled())
            td.cycles_undo_move += __rdtsc() - t0;
    }

#else // production â€” zero overhead

    static inline int timed_nnue(ThreadData &td)
    {
        Key k = td.board.key();
        auto &slot = td.eval_cache[k & ThreadData::EVAL_CACHE_MASK];
        if (slot.key == k)
            return slot.score;
        int r = NNUE::nnue_evaluate(td.board);
        slot.key = k;
        slot.score = r;
        return r;
    }
    static inline void timed_movegen(ThreadData &td, MoveList &moves, GenType gt = ALL_MOVES)
    {
        generate_legal(td.board, moves, gt);
    }
    static inline bool timed_see(ThreadData &td, Move m, int threshold)
    {
        return td.board.see_ge(m, threshold);
    }
    static inline bool timed_gcheck(ThreadData &td, Move m)
    {
        return td.board.gives_check(m);
    }
    static inline void timed_do_move(ThreadData &td, Move m, StateInfo &st)
    {
        td.board.do_move(m, st);
    }
    static inline void timed_undo_move(ThreadData &td, Move m)
    {
        td.board.undo_move(m);
    }

#endif // PROFILE

    SearchEngine Search;

    SearchEngine::SearchEngine()
    {
        static bool once = (init_lmr(), true);
        (void)once;
        set_threads(1);
    }

    void SearchEngine::rebuild_lmr()
    {
        init_lmr();
    }

    SearchEngine::~SearchEngine()
    {
        shutdown_pool();
        stop();
    }

    void SearchEngine::shutdown_pool()
    {
        {
            std::lock_guard<std::mutex> lk(pool_mutex_);
            pool_active_ = false;
            pool_go_ = false;
        }
        pool_cv_.notify_all();
        for (auto &t : pool_threads_)
        {
            if (t.joinable())
                t.join();
        }
        pool_threads_.clear();
    }

    void SearchEngine::set_threads(int n)
    {
        // Shut down existing pool threads before resizing
        shutdown_pool();

        n = std::max(1, std::min(n, 256));
        num_threads_ = n;
        thread_data_.clear();
        for (int i = 0; i < n; ++i)
        {
            thread_data_.push_back(std::make_unique<ThreadData>());
            thread_data_.back()->thread_id = i;
        }

        // Start persistent pool threads (used by ROOT_SPLIT mode)
        if (n > 1)
        {
            pool_active_ = true;
            pool_go_ = false;
            for (int i = 1; i < n; ++i)
            {
                pool_threads_.emplace_back(&SearchEngine::worker_thread_loop, this, i);
            }
        }
    }

    // ============================================================================
    // Time management
    // ============================================================================

    int SearchEngine::allocate_time(const SearchLimits &limits, Color side)
    {
        if (limits.movetime > 0)
        {
            soft_time_ms_ = limits.movetime;
            hard_time_ms_ = limits.movetime;
            return limits.movetime;
        }

        if (limits.infinite || limits.depth < MAX_PLY)
        {
            soft_time_ms_ = 0;
            hard_time_ms_ = 0;
            return 0; // No time limit
        }

        int time_left = (side == WHITE) ? limits.wtime : limits.btime;
        int increment = (side == WHITE) ? limits.winc : limits.binc;

        if (time_left <= 0)
        {
            soft_time_ms_ = 500;
            hard_time_ms_ = 1000;
            return 1000;
        }

        // Emergency mode: very low clock -- spend a tiny slice and protect the rest
        if (time_left < 2000)
        {
            int base = time_left * 5 / 100;
            int inc_bonus = increment / 2;
            int movetime = base + inc_bonus;
            movetime = std::min(movetime, (time_left + increment) / 4);
            movetime = std::min(movetime, 500);
            movetime = std::max(movetime, 20);
            soft_time_ms_ = movetime;
            hard_time_ms_ = movetime;
            return movetime;
        }

        // Speed-aware parameters derived from clock magnitude
        int expected_moves;
        int min_buffer;
        double max_fraction;
        if (time_left < 30000)
        { // bullet
            expected_moves = 40;
            min_buffer = 25;
            max_fraction = 0.04;
        }
        else if (time_left < 180000)
        { // blitz
            expected_moves = 35;
            min_buffer = 15;
            max_fraction = 0.07;
        }
        else
        { // rapid / classical
            expected_moves = 30;
            min_buffer = 10;
            max_fraction = 0.10;
        }

        int moves_left;
        if (limits.movestogo > 0)
            moves_left = limits.movestogo;
        else
            moves_left = expected_moves;

        moves_left = std::max(moves_left, min_buffer);

        // Thoughtfulness [0-100]: how aggressively we spend time.
        // t=0 -> lean (0.60 * fair_share), t=100 -> generous (1.40 * fair_share)
        double t = thoughtfulness_ / 100.0;
        double fair_share = double(time_left) / moves_left + increment;
        double target = fair_share * (0.60 + t * 0.80);

        const int OVERHEAD_MS = 150;
        double hard_cap = time_left * max_fraction;

        int soft = int(std::min({target, hard_cap, double(time_left - OVERHEAD_MS)}));
        soft = std::max(soft, 10);

        // Hard limit: 2.5x soft, never more than 50% of remaining clock
        int hard = int(std::min(double(soft) * 2.5, time_left * 0.50));
        hard = std::max(hard, soft);

        soft_time_ms_ = soft;
        hard_time_ms_ = hard;
        return soft;
    }

    bool SearchEngine::should_stop(const ThreadData &td) const
    {
        if (stop_flag_.load(std::memory_order_relaxed))
            return true;
        if (external_stop_ && external_stop_->load(std::memory_order_relaxed))
            return true;

        // While pondering, never stop on time â€” only stop_flag stops us
        if (pondering_.load(std::memory_order_relaxed))
            return false;

        // Check time every 4096 nodes â€” use hard limit
        if (hard_time_ms_ > 0 && (td.nodes & 4095) == 0)
        {
            if (main_stats_.elapsed_ms() >= hard_time_ms_)
            {
                // Set stop_flag_ so root-parallel loops (which check stop_flag_
                // directly rather than calling should_stop) also terminate.
                stop_flag_.store(true, std::memory_order_relaxed);
                return true;
            }
        }
        return false;
    }

    void SearchEngine::ponderhit(int wtime, int btime, int winc, int binc, int movestogo)
    {
        // Transition from ponder search to timed search
        if (!pondering_.load())
            return;

        // Use provided clocks if any; otherwise fall back to the saved limits
        // from the original go ponder command.
        SearchLimits lim;
        if (wtime > 0 || btime > 0)
        {
            lim.wtime = wtime;
            lim.btime = btime;
            lim.winc = winc;
            lim.binc = binc;
            lim.movestogo = movestogo;
        }
        else
        {
            lim = saved_limits_;
        }
        int alloc = allocate_time(lim, ponder_side_);
        max_time_ms_ = alloc;
        // soft_time_ms_ and hard_time_ms_ are set inside allocate_time()

        // Reset the start time so the time budget starts from NOW
        main_stats_.start_time = std::chrono::steady_clock::now();

        // Clear ponder flag â€” should_stop will now obey the clock,
        // AND the spin-wait at the end of search() will fall through.
        pondering_.store(false, std::memory_order_release);
    }

    // ============================================================================
    // Move ordering
    // ============================================================================

    void SearchEngine::score_moves(ThreadData &td, MoveList &moves, Move tt_move, int ply, Move prev_move)
    {
        // Determine countermove for previous move
        Move counter = Move::none();
        if (prev_move)
        {
            Piece prev_piece = td.board.piece_on(prev_move.to()); // Piece that moved (now on 'to')
            // After opponent's move, the piece is on the board at prev_move.to()
            // But we need the piece that the OPPONENT moved â€” which is now on prev_move.to()
            // Actually we look from the current side's perspective at the previous side's piece
            if (prev_piece != NO_PIECE)
                counter = td.countermove[prev_piece][prev_move.to()];
        }

        for (int i = 0; i < moves.size(); ++i)
        {
            Move m = moves[i].move;

            if (m == tt_move)
            {
                moves[i].score = 10000000; // TT move first
            }
            else if (td.board.is_capture(m))
            {
                // Good captures first (SEE >= 0), bad captures last
                Piece victim = td.board.piece_on(m.to());
                Piece attacker = td.board.piece_on(m.from());
                PieceType cap_type = (victim != NO_PIECE) ? type_of(victim) : PAWN; // EP â†’ pawn
                int cap_hist = td.capture_history[attacker][m.to()][cap_type];
                if (timed_see(td, m, 0))
                {
                    // MVV-LVA for good captures, boosted by capture history
                    int victim_val = victim != NO_PIECE ? int(type_of(victim)) * 100 : 0;
                    int attacker_val = int(type_of(attacker));
                    moves[i].score = 1000000 + victim_val * 10 - attacker_val + cap_hist / 16;
                }
                else
                {
                    // Bad-SEE captures: score below killers.
                    moves[i].score = -1000000 + cap_hist / 16;
                }
            }
            else if (m == td.killers[ply][0])
            {
                moves[i].score = 900000;
            }
            else if (m == td.killers[ply][1])
            {
                moves[i].score = 800000;
            }
            else if (m == counter)
            {
                moves[i].score = 700000; // Countermove heuristic
            }
            else
            {
                // History heuristic + continuation history
                Color c = td.board.side_to_move();
                int hist_score = td.history[c][m.from()][m.to()];

                // 1-ply continuation history: bonus based on previous move's piece/to
                if (prev_move)
                {
                    Piece pp = td.board.piece_on(prev_move.to());
                    Piece cp = td.board.piece_on(m.from());
                    if (pp != NO_PIECE && cp != NO_PIECE)
                        hist_score += td.conthist[type_of(pp)][prev_move.to()][type_of(cp)][m.to()];
                }

                // 2-ply continuation history: bonus based on move 2 plies ago
                if (ply >= 2)
                {
                    const auto &pi2 = td.ply_info[ply - 2];
                    if (pi2.moved_piece != NO_PIECE)
                    {
                        Piece cp = td.board.piece_on(m.from());
                        if (cp != NO_PIECE)
                            hist_score += td.conthist2[type_of(pi2.moved_piece)][pi2.current_move.to()][type_of(cp)][m.to()];
                    }
                }

                moves[i].score = hist_score;
            }
        }
    }

    namespace
    {
        // Pick the best-scoring move and swap it to position 'index'
        void pick_move(MoveList &moves, int index)
        {
            int best = index;
            for (int i = index + 1; i < moves.size(); ++i)
            {
                if (moves[i].score > moves[best].score)
                    best = i;
            }
            if (best != index)
                std::swap(moves[index], moves[best]);
        }
    } // anonymous namespace

    // ============================================================================
    // Quiescence Search â€” resolve tactical sequences
    // ============================================================================

    int SearchEngine::quiescence(ThreadData &td, int alpha, int beta, int ply)
    {
        if (should_stop(td))
            return 0;

        td.nodes++;
        td.diag.quiescence_nodes++;
        td.seldepth = std::max(td.seldepth, ply);

        // Safety: stop recursion if ply exceeds array bounds
        if (ply >= MAX_PLY - 1)
            return timed_nnue(td);

        bool in_check = td.board.in_check();

        // Stand pat: only meaningful when NOT in check.
        // When in check we must try all evasions.
        int stand_pat = 0;
        if (!in_check)
        {
            stand_pat = timed_nnue(td);
            if (stand_pat >= beta)
                return beta;
            if (stand_pat > alpha)
                alpha = stand_pat;
        }

        // Generate moves:
        //  â€¢ In check  â†’ all legal evasions (captures + quiets)
        //  â€¢ otherwise â†’ captures only
        MoveList moves;
        if (in_check)
        {
            timed_movegen(td, moves, ALL_MOVES);
        }
        else
        {
            timed_movegen(td, moves, CAPTURES_ONLY);
        }
        score_moves(td, moves, Move::none(), ply);

        // In-check with no legal moves â†’ checkmate
        if (in_check && moves.size() == 0)
            return -VALUE_MATE + ply;

        // Search captures first
        for (int i = 0; i < moves.size(); ++i)
        {
            pick_move(moves, i);
            Move m = moves[i].move;
            bool is_capture = td.board.is_capture(m);

            // Delta pruning: cheap material-gain test first.
            if (!in_check && is_capture)
            {
                constexpr int DELTA_MARGIN = 200;
                Piece victim = td.board.piece_on(m.to());
                int gain = (victim != NO_PIECE) ? PIECE_VALUES[type_of(victim)] : PIECE_VALUES[PAWN];
                if (stand_pat + DELTA_MARGIN + gain <= alpha && !timed_gcheck(td, m))
                    continue;
            }

            // SEE pruning: skip losing captures UNLESS in check (all evasions
            // must be searched) or the move gives check.
            if (!in_check && is_capture && !timed_see(td, m, 0) && !timed_gcheck(td, m))
                continue;

            StateInfo &state = td.states[ply];
            timed_do_move(td, m, state);
            int score = -quiescence(td, -beta, -alpha, ply + 1);
            timed_undo_move(td, m);

            if (should_stop(td))
                return 0;

            if (score >= beta)
                return beta;
            if (score > alpha)
                alpha = score;
        }

        return alpha;
    }

    // ============================================================================
    // Alpha-Beta Search with PVS (Principal Variation Search)
    // ============================================================================

    int SearchEngine::alpha_beta(ThreadData &td, int depth, int alpha, int beta, int ply, bool is_pv, bool cutNode, Move prev_move)
    {
        if (should_stop(td))
            return 0;

        // Quiescence at leaf
        if (depth <= 0)
            return quiescence(td, alpha, beta, ply);

        td.nodes++;
        td.diag.alpha_beta_nodes++;
        td.seldepth = std::max(td.seldepth, ply);

        // Draw detection
        if (ply > 0 && td.board.is_draw())
            return VALUE_DRAW;

        // Max ply check
        if (ply >= MAX_PLY - 1)
            return timed_nnue(td);

        // Transposition Table probe
        TTEntry tt_entry;
        Move tt_move = Move::none();
        const bool use_tt = tt_enabled_;
        bool tt_hit = false;
        if (use_tt)
        {
            td.diag.tt_probes++;
            tt_hit = TT.probe(td.board.key(), tt_entry);
            if (tt_hit)
                td.diag.tt_hits++;
        }

        if (tt_hit)
        {
            tt_move = tt_entry.move;
            if (!is_pv && tt_entry.depth >= depth)
            {
                int tt_score = tt_entry.score;
                if (tt_entry.flag == TT_EXACT)
                {
                    td.diag.tt_cutoffs++;
                    return tt_score;
                }
                if (tt_entry.flag == TT_BETA && tt_score >= beta)
                {
                    td.diag.tt_cutoffs++;
                    return tt_score;
                }
                if (tt_entry.flag == TT_ALPHA && tt_score <= alpha)
                {
                    td.diag.tt_cutoffs++;
                    return tt_score;
                }
            }
        }

        bool in_check = td.board.in_check();

        // Syzygy tablebase probe (interior nodes)
        // Fathom WDL returns FAILED when castling rights != 0 or rule50 != 0, so
        // this is a no-op in those positions — perfectly safe to call unconditionally.
        if (!is_pv && syzygy_enabled() && depth >= syzygy_probe_depth() && popcount(td.board.pieces()) <= syzygy_piece_limit())
        {
            int wdl = syzygy_probe_wdl(td.board);
            if (wdl != INT_MIN)
            {
                td.diag.tb_hits++;
                // cursed win / blessed loss: treat as draw (50-move rule will claim it)
                int tb_score = (wdl >= 2)    ? (VALUE_TB_WIN - ply)
                               : (wdl <= -2) ? (VALUE_TB_LOSS + ply)
                                             : VALUE_DRAW;
                TTFlag tb_flag = (tb_score >= beta)    ? TT_BETA
                                 : (tb_score <= alpha) ? TT_ALPHA
                                                       : TT_EXACT;
                TT.store(td.board.key(), Move::none(), tb_score, depth, tb_flag);
                return tb_score;
            }
        }

        // Static evaluation (used by multiple pruning techniques)
        int raw_eval = in_check ? -VALUE_INFINITE : timed_nnue(td);
        int static_eval = raw_eval;

        // Correction history: adjust static eval based on pawn-structure-specific error history
        if (!in_check)
        {
            int corr_idx = int(td.board.pawn_key() % ThreadData::CORR_HIST_SIZE);
            static_eval += td.pawn_correction[corr_idx] / ThreadData::CORR_GRAIN;
        }

        // Store eval for "improving" detection
        td.eval_stack[ply] = static_eval;

        // "Improving": is our static eval better than 2 plies ago?
        // When not improving, pruning is more aggressive.
        bool improving = !in_check && ply >= 2 && static_eval > td.eval_stack[ply - 2];

        // Reverse Futility Pruning (Static Null Move Pruning)
        // If our position is so good that even with a margin we still beat beta,
        // prune this node (not in check, not PV, no dangerous conditions)
        if (!is_pv && !in_check && depth <= SP.rfp_max_depth && ply > 0)
        {
            int rfp_margin = SP.rfp_margin * depth - SP.rfp_improving_sub * improving;
            if (static_eval - rfp_margin >= beta)
            {
                td.diag.rfp_cuts++;
                return static_eval;
            }
        }

        // Probcut: if a shallow search with raised beta proves the position is winning, prune
        if (!is_pv && !in_check && depth >= SP.probcut_min_depth && std::abs(beta) < VALUE_MATE_IN_MAX_PLY)
        {
            int probcut_beta = beta + SP.probcut_beta_margin;
            if (td.board.non_pawn_material(td.board.side_to_move()) > 0)
            {
                // Do a shallow search at reduced depth
                MoveList pc_moves;
                timed_movegen(td, pc_moves, CAPTURES_ONLY);
                score_moves(td, pc_moves, tt_move, ply, prev_move);
                for (int i = 0; i < pc_moves.size(); ++i)
                {
                    pick_move(pc_moves, i);
                    Move m = pc_moves[i].move;
                    // SEE threshold scaled by capture history (proven good/bad captures get slack/penalty)
                    {
                        Piece pc_att = td.board.piece_on(m.from());
                        Piece pc_vic = td.board.piece_on(m.to());
                        PieceType pc_ctype = (pc_vic != NO_PIECE) ? type_of(pc_vic) : PAWN;
                        int pc_capt_hist = td.capture_history[pc_att][m.to()][pc_ctype];
                        if (!timed_see(td, m, probcut_beta - static_eval - pc_capt_hist / 32))
                            continue;
                    }
                    StateInfo &pc_state = td.states[ply];
                    timed_do_move(td, m, pc_state);
                    int pc_score = -alpha_beta(td, depth - 4, -probcut_beta, -probcut_beta + 1, ply + 1, false, !cutNode, m);
                    timed_undo_move(td, m);
                    if (pc_score >= probcut_beta)
                    {
                        td.diag.probcut_cuts++;
                        return pc_score;
                    }
                }
            }
        }

        // Razoring: if static eval is far below alpha at shallow depth,
        // verify with quiescence â€” if still below, prune.
        if (!is_pv && !in_check && depth <= SP.razor_max_depth && ply > 0 && static_eval + SP.razor_base + SP.razor_depth_scale * (depth - 1) <= alpha)
        {
            int razor_score = quiescence(td, alpha, beta, ply);
            if (razor_score <= alpha)
            {
                td.diag.razoring_cuts++;
                return razor_score;
            }
        }

        // Null Move Pruning (skip when in check or at low depth)
        if (!is_pv && !in_check && depth >= SP.nmp_min_depth && static_eval >= beta)
        {
            // Make a "null move" â€” pass the turn
            // Only if we have non-pawn material (avoids zugzwang in pawn endings)
            if (td.board.non_pawn_material(td.board.side_to_move()) > 0)
            {
                int R = SP.nmp_base_r + depth / SP.nmp_depth_div + std::min(SP.nmp_max_bonus, (static_eval - beta) / SP.nmp_eval_div);
                StateInfo &null_state = td.states[ply];
                td.board.do_null_move(null_state);
                int null_score = -alpha_beta(td, depth - 1 - R, -beta, -beta + 1, ply + 1, false, !cutNode);
                td.board.undo_null_move();

                if (null_score >= beta)
                {
                    td.diag.null_move_cuts++;
                    // Don't return unproven mate scores
                    if (null_score >= VALUE_MATE_IN_MAX_PLY)
                        null_score = beta;
                    return null_score;
                }
            }
        }

        // Internal Iterative Reduction (IIR)
        // When we have no TT move, reduce depth â€” cheaper than IID and
        // works at ALL node types (not just PV).
        if (!tt_move && depth >= SP.iir_min_depth)
            depth -= 1;

        // Singular Extension: when TT move is significantly better than all
        // alternatives, extend its search depth by one ply.
        int singular_extension = 0;
        if (ply > 0 && depth >= SP.se_min_depth && tt_move && tt_hit && !in_check && td.excluded[ply] == Move::none() && tt_entry.depth >= depth - SP.se_tt_depth_margin && (tt_entry.flag == TT_BETA || tt_entry.flag == TT_EXACT) && std::abs(tt_entry.score) < VALUE_MATE_IN_MAX_PLY)
        {
            int singular_beta = tt_entry.score - SP.se_beta_scale * depth;
            int singular_depth = (depth - 1) / 2;

            td.excluded[ply] = tt_move;
            int se_score = alpha_beta(td, singular_depth, singular_beta - 1, singular_beta, ply, false, cutNode, prev_move);
            td.excluded[ply] = Move::none();

            if (se_score < singular_beta)
            {
                singular_extension = 1;
            }
            else if (singular_beta >= beta)
            {
                // Multi-cut: TT move is NOT singular AND singular_beta >= beta,
                // meaning multiple moves beat beta â€” this is a cut-node, return early.
                return singular_beta;
            }
        }

        // Generate and score moves
        MoveList moves;
        timed_movegen(td, moves);

        // No legal moves: checkmate or stalemate
        if (moves.size() == 0)
        {
            if (in_check)
                return -VALUE_MATE + ply; // Checkmate
            return VALUE_DRAW;            // Stalemate
        }

        score_moves(td, moves, tt_move, ply, prev_move);

        // Root move ordering diagnostic: emit scored move list at depth d
        if (ply == 0 && diag_enabled_)
        {
            // Sort a copy to show the ordering the search will use
            std::vector<std::pair<int, std::string>> order;
            order.reserve(moves.size());
            for (int i = 0; i < moves.size(); ++i)
                order.push_back({moves[i].score, moves[i].move.to_uci()});
            std::sort(order.begin(), order.end(), [](auto &a, auto &b)
                      { return a.first > b.first; });
            std::ostringstream oss;
            oss << "info string moveorder depth " << td.root_depth;
            for (auto &[s, m] : order)
                oss << " " << m << ":" << s;
            std::cout << oss.str() << std::endl;
        }

        Move best_move = Move::none();
        int best_score = -VALUE_INFINITE;
        TTFlag tt_flag = TT_ALPHA;
        int moves_searched = 0;

        // Track quiet moves searched (for history malus on cutoff)
        Move quiets_searched[64];
        int num_quiets_searched = 0;
        Move captures_searched[32];
        int num_captures_searched = 0;

        // Futility pruning flag: can we skip quiet moves at low depths?
        // Extended to depth<=7 with a scaled margin: 85*depth gives ~600cp at depth 7.
        bool futility_prunable = !is_pv && !in_check && depth <= SP.fp_max_depth && static_eval + (SP.fp_base + SP.fp_depth_scale * depth - SP.fp_improving_sub * improving) <= alpha;

        for (int i = 0; i < moves.size(); ++i)
        {
            pick_move(moves, i);
            Move m = moves[i].move;

            // Skip excluded move (for singular extension search)
            if (m == td.excluded[ply])
                continue;

            bool is_quiet = !td.board.is_capture(m) && m.type() != PROMOTION;

            // Compute aggregated stat_score early â€” used for both pruning and LMR
            Piece moving_piece = td.board.piece_on(m.from());
            int move_stat_score = 0;
            if (!is_quiet)
            {
                // For captures: use capture history to drive LMR scaling
                Piece cap_vic = td.board.piece_on(m.to());
                PieceType ctype = (cap_vic != NO_PIECE) ? type_of(cap_vic) : PAWN;
                move_stat_score = td.capture_history[moving_piece][m.to()][ctype];
            }
            else
            {
                Color us = td.board.side_to_move();
                move_stat_score = td.history[us][m.from()][m.to()];
                // 1-ply continuation history
                if (prev_move)
                {
                    Piece pp = td.board.piece_on(prev_move.to());
                    if (pp != NO_PIECE && moving_piece != NO_PIECE)
                        move_stat_score += td.conthist[type_of(pp)][prev_move.to()][type_of(moving_piece)][m.to()];
                }
                // 2-ply continuation history
                if (ply >= 2)
                {
                    const auto &pi2 = td.ply_info[ply - 2];
                    if (pi2.moved_piece != NO_PIECE && moving_piece != NO_PIECE)
                        move_stat_score += td.conthist2[type_of(pi2.moved_piece)][pi2.current_move.to()][type_of(moving_piece)][m.to()];
                }
            }

            // Futility pruning: skip quiet moves at shallow depths when eval is far below alpha
            if (futility_prunable && moves_searched > 0 && is_quiet && !timed_gcheck(td, m))
            {
                td.diag.futility_prunes++;
                continue;
            }

            // Late Move Pruning (LMP): at shallow depths, skip late quiet moves
            // Extended to depth<=7 â€” at depth 7, threshold is ~52/54, still very safe.
            if (!is_pv && !in_check && depth <= SP.lmp_max_depth && is_quiet && moves_searched >= (improving ? SP.lmp_improving_base : SP.lmp_base) + depth * depth)
            {
                td.diag.lmp_prunes++;
                continue;
            }

            // SEE pruning for quiet moves â€” threshold scales with depthÂ².
            // No depth gate: at depth 20 threshold is -8000 which still catches
            // egregiously bad moves while being safe at high depth.
            if (!is_pv && is_quiet && moves_searched > 0 && !timed_see(td, m, -SP.see_quiet_scale * depth * depth))
            {
                td.diag.see_prunes++;
                continue;
            }

            // History-based quiet pruning: skip quiet moves whose aggregated
            // stat_score is terrible. These moves have consistently been bad
            // across history and conthist.
            if (!is_pv && is_quiet && depth <= SP.hist_prune_max_depth && moves_searched > 0 && move_stat_score < -SP.hist_prune_scale * depth)
            {
                td.diag.hist_prunes++;
                continue;
            }

            // SEE pruning for losing captures at all depths.
            // Capture history adjusts the threshold: repeatedly-good captures get slack,
            // repeatedly-bad captures are pruned more eagerly.
            if (!is_pv && !is_quiet && moves_searched > 0)
            {
                Piece cap_att = td.board.piece_on(m.from());
                Piece cap_vic = td.board.piece_on(m.to());
                PieceType ctype_see = (cap_vic != NO_PIECE) ? type_of(cap_vic) : PAWN;
                int capt_hist = td.capture_history[cap_att][m.to()][ctype_see];
                int see_hist_adj = std::clamp(capt_hist / SP.see_capt_hist_div, -SP.see_capt_hist_max * depth, SP.see_capt_hist_min * depth);
                if (!timed_see(td, m, -SP.see_capt_scale * depth - see_hist_adj))
                {
                    td.diag.see_prunes++;
                    continue;
                }
            }

            // Per-move extensions (allow double extension for truly critical moves)
            // Gate on ply < 2 * root_depth to prevent search explosion.
            // Check extension is handled here at per-move level so it shares the
            // extension slot (via std::max) with other extensions. This prevents
            // stacking that caused depth to grow in cascading check/promotion lines.
            int extension = 0;
            if (ply < 2 * td.root_depth)
            {
                // Check extension: when *this node* is in check, extend by 1
                if (in_check)
                    extension = std::max(extension, 1);
                // Singular extension: TT move proven to be the only good move
                if (m == tt_move)
                    extension = std::max(extension, singular_extension);
                if (m.type() == PROMOTION)
                    extension = std::max(extension, 1);
                if (td.board.is_capture(m) && prev_move && m.to() == prev_move.to())
                    extension = std::max(extension, 1);
                // Passed pawn extension: advanced passers (rank 6/7 for white, rank 2/3 for black)
                // about to promote deserve extra search depth to avoid tactical oversights.
                if (extension == 0 && type_of(td.board.piece_on(m.from())) == PAWN)
                {
                    Color stm = td.board.side_to_move();
                    Rank r = rank_of(m.to());
                    bool advanced = (stm == WHITE) ? r >= RANK_6 : r <= RANK_3;
                    if (advanced && !(td.board.pieces(~stm, PAWN) & passed_pawn_mask(stm, m.to())))
                        extension = 1;
                }
                // Double extension: singular move while in check (truly forced critical line)
                if (singular_extension && m == tt_move && in_check)
                    extension = std::min(extension + 1, 2);
            }

            StateInfo &state = td.states[ply];
            timed_do_move(td, m, state);

            // Prefetch the TT bucket for the child position â€” warms the cache
            // so the TT probe at the top of the child's alpha_beta is fast.
            if (use_tt)
                TT.prefetch(td.board.key());

            // Store ply info for sub-searches' continuation history lookback
            td.ply_info[ply].current_move = m;
            td.ply_info[ply].moved_piece = td.board.piece_on(m.to());

            int score;

            int new_depth = depth - 1 + extension;

            // PVS: Search the first move with full window, rest with null window
            if (moves_searched == 0)
            {
                // First move: full window, child is PV node (not a cut-node)
                score = -alpha_beta(td, new_depth, -beta, -alpha, ply + 1, is_pv, false, m);
            }
            else
            {
                // Late Move Reductions (LMR) â€” log-based with history scaling
                int reduction = 0;
                if (depth >= SP.lmr_min_depth && moves_searched >= SP.lmr_min_moves && !in_check)
                {
                    // Pre-computed log-based reduction
                    reduction = lmr_table[std::min(depth, 127)][std::min(moves_searched, 255)];

                    // Reduce less in PV nodes
                    if (is_pv)
                        reduction -= SP.lmr_pv_sub;

                    if (is_quiet)
                    {
                        // Reduce more when position is NOT improving
                        if (!improving)
                            reduction += SP.lmr_improving_add;
                    }
                    else
                    {
                        // Captures are generally more forcing than quiets â€” reduce 1 less
                        reduction -= 1;
                    }

                    // Cut-nodes: child is expected to fail high, search more aggressively
                    if (cutNode)
                        reduction += SP.lmr_cutnode_add;

                    // Scale by aggregated stat_score (history + conthist): good moves -> reduce less
                    reduction -= move_stat_score / SP.lmr_hist_div;

                    // Extra reduction for very poor stat_score
                    if (move_stat_score < SP.lmr_hist_bad_thresh)
                        reduction += SP.lmr_hist_bad_add;

                    // Reduce less if this move gives check to the opponent
                    if (SP.lmr_check_sub && td.board.in_check())
                        reduction -= 1;

                    // Clamp: ensure at least depth-1 search
                    reduction = std::max(0, std::min(reduction, new_depth - 1));
                }

                // Null-window search with reduction â€” child of null-window is a cut-node
                if (reduction > 0)
                    td.diag.lmr_searches++;
                score = -alpha_beta(td, new_depth - reduction, -alpha - 1, -alpha, ply + 1, false, true, m);

                // Re-search if reduced search found something interesting
                if (score > alpha && reduction > 0)
                {
                    td.diag.lmr_re_searches++;
                    score = -alpha_beta(td, new_depth, -alpha - 1, -alpha, ply + 1, false, !cutNode, m);
                }

                // Full re-search if null-window search beat alpha â€” child is PV
                if (score > alpha && score < beta)
                    score = -alpha_beta(td, new_depth, -beta, -alpha, ply + 1, true, false, m);
            }

            timed_undo_move(td, m);
            moves_searched++;

            // Track quiet moves for history malus
            if (is_quiet && num_quiets_searched < 64)
                quiets_searched[num_quiets_searched++] = m;
            if (!is_quiet && num_captures_searched < 32)
                captures_searched[num_captures_searched++] = m;

            if (should_stop(td))
                return 0;

            if (score > best_score)
            {
                best_score = score;
                best_move = m;

                if (score > alpha)
                {
                    alpha = score;
                    tt_flag = TT_EXACT;

                    if (score >= beta)
                    {
                        tt_flag = TT_BETA;

                        // Update killer moves, history, and countermove (quiet moves only)
                        if (is_quiet)
                        {
                            td.killers[ply][1] = td.killers[ply][0];
                            td.killers[ply][0] = m;

                            // History bonus for the cutoff move (gravity-based, SF-scale)
                            Color c = td.board.side_to_move();
                            int bonus = history_bonus(depth);
                            update_history(td.history[c][m.from()][m.to()], bonus);

                            // History malus: penalize all other quiet moves (gravity-based)
                            for (int q = 0; q < num_quiets_searched - 1; ++q)
                            {
                                Move qm = quiets_searched[q];
                                update_history(td.history[c][qm.from()][qm.to()], -bonus);
                            }

                            // Update countermove table
                            if (prev_move)
                            {
                                Piece prev_piece = td.board.piece_on(prev_move.to());
                                if (prev_piece != NO_PIECE)
                                    td.countermove[prev_piece][prev_move.to()] = m;
                            }

                            // Update 1-ply continuation history
                            if (prev_move)
                            {
                                Piece pp = td.board.piece_on(prev_move.to());
                                Piece cp = td.board.piece_on(m.from());
                                if (pp != NO_PIECE && cp != NO_PIECE)
                                {
                                    update_history(td.conthist[type_of(pp)][prev_move.to()][type_of(cp)][m.to()], bonus);
                                    // Malus for other quiet moves in continuation history
                                    for (int q = 0; q < num_quiets_searched - 1; ++q)
                                    {
                                        Move qm = quiets_searched[q];
                                        Piece qp = td.board.piece_on(qm.from());
                                        if (qp != NO_PIECE)
                                            update_history(td.conthist[type_of(pp)][prev_move.to()][type_of(qp)][qm.to()], -bonus);
                                    }
                                }
                            }

                            // Update 2-ply continuation history
                            if (ply >= 2)
                            {
                                const auto &pi2 = td.ply_info[ply - 2];
                                if (pi2.moved_piece != NO_PIECE)
                                {
                                    Piece cp = td.board.piece_on(m.from());
                                    if (cp != NO_PIECE)
                                    {
                                        update_history(td.conthist2[type_of(pi2.moved_piece)][pi2.current_move.to()][type_of(cp)][m.to()], bonus);
                                        // Malus for other quiet moves in 2-ply continuation history
                                        for (int q = 0; q < num_quiets_searched - 1; ++q)
                                        {
                                            Move qm = quiets_searched[q];
                                            Piece qp = td.board.piece_on(qm.from());
                                            if (qp != NO_PIECE)
                                                update_history(td.conthist2[type_of(pi2.moved_piece)][pi2.current_move.to()][type_of(qp)][qm.to()], -bonus);
                                        }
                                    }
                                }
                            }
                        }
                        else
                        {
                            // Capture cutoff: update capture history
                            int bonus = history_bonus(depth);
                            Piece attacker = td.board.piece_on(m.from());
                            // After do_move, victim is gone â€” but we haven't done the move here,
                            // actually we have undone it. We need the captured piece type.
                            // Recalculate: the move captured the piece that was on m.to()
                            // but the board has been undone, so piece_on(m.to()) gives the victim.
                            Piece victim = td.board.piece_on(m.to());
                            PieceType cap_type = (victim != NO_PIECE) ? type_of(victim) : PAWN;
                            update_history(td.capture_history[attacker][m.to()][cap_type], bonus);

                            // Malus for other captures that didn't cut
                            for (int q = 0; q < num_captures_searched - 1; ++q)
                            {
                                Move cm = captures_searched[q];
                                Piece ca = td.board.piece_on(cm.from());
                                Piece cv = td.board.piece_on(cm.to());
                                PieceType ct = (cv != NO_PIECE) ? type_of(cv) : PAWN;
                                update_history(td.capture_history[ca][cm.to()][ct], -bonus);
                            }
                        }
                        break;
                    }
                }
            }
        }

        // Update correction history: record how far off our raw static eval was
        if (!in_check && best_score > -VALUE_INFINITE && ply > 0 && std::abs(best_score) < VALUE_MATE_IN_MAX_PLY)
        {
            int corr_idx = int(td.board.pawn_key() % ThreadData::CORR_HIST_SIZE);
            int error = best_score - raw_eval;
            int weight = std::min(depth + 1, 16);
            td.pawn_correction[corr_idx] =
                (td.pawn_correction[corr_idx] * (ThreadData::CORR_LIMIT - weight) + error * ThreadData::CORR_GRAIN * weight) / ThreadData::CORR_LIMIT;
            td.pawn_correction[corr_idx] = std::max(-ThreadData::CORR_MAX,
                                                    std::min(ThreadData::CORR_MAX, td.pawn_correction[corr_idx]));
        }

        // Store in transposition table
        if (use_tt)
            TT.store(td.board.key(), best_move, best_score, depth, tt_flag);

        return best_score;
    }

    // ============================================================================
    // Lazy SMP worker â€” runs independent iterative deepening to pollute TT
    // ============================================================================

    void SearchEngine::worker_lazy_smp(int thread_id, Board board, const SearchLimits &limits)
    {
        auto &td = *thread_data_[thread_id];
        td.board = board;
        td.soft_reset();

        int max_depth = std::min(limits.depth, MAX_PLY - 1);

        // Depth staggering: spread threads across 4 distinct start depths.
        // With 8 threads this gives 2 threads per depth-offset instead of 4.
        int start_depth = 1 + (thread_id % 4);

        for (int depth = start_depth; depth <= max_depth; ++depth)
        {
            if (stop_flag_.load(std::memory_order_relaxed))
                break;

            td.seldepth = 0;
            td.root_depth = depth;
            td.diag.clear();

            // Simple aspiration windows (same as main thread)
            int alpha = -VALUE_INFINITE;
            int beta = VALUE_INFINITE;

            alpha_beta(td, depth, alpha, beta, 0, true);

            if (stop_flag_.load(std::memory_order_relaxed))
                break;
        }
    }

    // ============================================================================
    // Persistent thread pool worker — sleeps between depth iterations.
    // Used by ROOT_SPLIT mode.  Each helper thread sits in this loop for the
    // lifetime of the engine process, waking when the main thread signals a new
    // depth iteration.
    // ============================================================================

    void SearchEngine::worker_thread_loop(int thread_id)
    {
        while (true)
        {
            // Sleep until main thread signals a new depth iteration (or shutdown)
            {
                std::unique_lock<std::mutex> lk(pool_mutex_);
                pool_cv_.wait(lk, [this]
                              { return pool_go_ || !pool_active_; });
                if (!pool_active_)
                    return; // clean shutdown
            }

            // --- Do work: search our assigned root moves at rs_depth_ ---
            if (!stop_flag_.load(std::memory_order_relaxed))
            {
                auto &td = *thread_data_[thread_id];
                root_split_search(td, rs_board_, rs_root_moves_, rs_depth_,
                                  thread_id, num_threads_);
            }

            // Signal completion
            {
                std::lock_guard<std::mutex> lk(pool_mutex_);
                pool_done_count_++;
            }
            pool_done_cv_.notify_one();
        }
    }

    // ============================================================================
    // Root-split search: search a round-robin subset of root moves for one depth.
    //
    // Each thread searches moves where (move_index % num_workers == thread_id).
    // The PV move (index 0) is always searched by thread 0 first to establish a
    // good alpha bound; other threads skip index 0.
    //
    // Threads share alpha via rs_best_score_ (atomically updated) so that later
    // moves benefit from earlier threads' cutoffs.
    // ============================================================================

    void SearchEngine::root_split_search(ThreadData &td, const Board &board,
                                         const MoveList &root_moves, int depth,
                                         int thread_id, int num_workers)
    {
        td.board = board;
        td.seldepth = 0;
        td.root_depth = depth;
        td.diag.clear();

        int n_moves = root_moves.size();

        for (int i = 0; i < n_moves; ++i)
        {
            if (stop_flag_.load(std::memory_order_relaxed))
                break;

            // Round-robin assignment: thread 0 owns moves 0, N, 2N, ...
            // Move 0 (PV move) is always thread 0's responsibility.
            if (i == 0 && thread_id != 0)
                continue;
            if (i > 0 && (i % num_workers) != thread_id)
                continue;

            Move m = root_moves[i].move;

            // Make the move on a thread-local board
            StateInfo st;
            td.board = board; // reset to root each time
            // Initialise ply-0 eval_stack so "improving" at ply 2 reads a sane value
            td.eval_stack[0] = 0;
            td.board.do_move(m, st);

            // Search with shared alpha from other threads
            int alpha = rs_best_score_.load(std::memory_order_relaxed);
            // For PV move at index 0, use full window
            if (i == 0)
                alpha = -VALUE_INFINITE;
            int beta = VALUE_INFINITE;

            int score = -alpha_beta(td, depth - 1, -beta, -alpha, 1, (i == 0),
                                    !(i == 0), m);

            td.board.undo_move(m);

            if (stop_flag_.load(std::memory_order_relaxed))
                break;

            // Update shared best score / best move
            {
                std::lock_guard<std::mutex> lk(rs_result_mutex_);
                if (score > rs_best_score_.load(std::memory_order_relaxed))
                {
                    rs_best_score_.store(score, std::memory_order_relaxed);
                    rs_best_move_ = m;
                }
            }
        }
    }

    // ============================================================================
    // Iterative Deepening â€” dispatcher
    // ============================================================================

    Move SearchEngine::search(Board &board, const SearchLimits &limits)
    {
        trace_search("enter mode=" + std::string(mode_ == ROOT_SPLIT ? "rootsplit" : "lazysmp") + " threads=" + std::to_string(num_threads_) + " depth=" + std::to_string(limits.depth));

        // ---- Setup ----
        stop_flag_.store(false);
        searching_.store(true);

        // Ponder mode: search indefinitely until stop/ponderhit
        pondering_.store(limits.ponder, std::memory_order_release);
        ponder_side_ = board.side_to_move();
        saved_limits_ = limits; // Save for ponderhit fallback

        main_stats_ = SearchStats{};
        main_stats_.start_time = std::chrono::steady_clock::now();
        last_profile_ = SearchProfile{};
        last_smp_ = SearchSmpStats{};
        if (profile_enabled())
            NNUE::nnue_profile_reset();

        // In ponder mode, don't set a time limit at first
        max_time_ms_ = limits.ponder ? 0 : allocate_time(limits, board.side_to_move());
        int max_depth = std::min(limits.depth, MAX_PLY - 1);

        TT.new_search();

        // Set up all threads
        for (int i = 0; i < num_threads_; ++i)
        {
            thread_data_[i]->age_history(); // decay history tables between searches
            thread_data_[i]->board = board;
            thread_data_[i]->soft_reset();
        }

        // Launch helper threads depending on search mode
        bool use_root_split = (mode_ == ROOT_SPLIT && num_threads_ > 1);
        if (!use_root_split)
        {
            // Lazy SMP: helper threads run independent ID loops
            threads_.clear();
            for (int i = 1; i < num_threads_; ++i)
            {
                threads_.emplace_back(&SearchEngine::worker_lazy_smp, this, i, board, limits);
            }
        }
        // ROOT_SPLIT: persistent pool threads are already running (started in set_threads)

        auto &main_td = *thread_data_[0];

        Move best_move = Move::none();
        int best_score = -VALUE_INFINITE;
        int prev_depth_score = -VALUE_INFINITE; // Previous iteration score for fallingEval
        int best_move_changes = 0;              // Track best move instability

        // Confidence gate: track eval stability + PV stability for early exit
        static constexpr int CONF_HIST = 8;
        double conf_eval_hist[CONF_HIST] = {};
        int conf_eval_idx = 0;
        int conf_stable_depths = 0;
        int conf_total_depths = 0;
        // Compute confidence threshold from Thoughtfulness + opp_time modifier
        double conf_threshold;
        {
            double t = thoughtfulness_ / 100.0;
            double thresh = 0.50 + t * 0.40;
            Color s = board.side_to_move();
            int my_time = (s == WHITE) ? limits.wtime : limits.btime;
            // Derive opponent time from limits (both wtime/btime are always provided
            // in a clock-based go command; opp_time field is used if explicitly given)
            int opp_time = limits.opp_time > 0
                               ? limits.opp_time
                               : ((s == WHITE) ? limits.btime : limits.wtime);
            if (opp_time > 0 && opp_time < 2000)
                thresh -= 0.08;
            else if (opp_time > 0 && opp_time < 5000)
                thresh -= 0.04;
            else if (opp_time > 0 && my_time > 0 &&
                     double(opp_time) / my_time > 3.0)
                thresh += 0.02;
            conf_threshold = std::clamp(thresh, 0.30, 0.99);
        }
        int min_conf_depth = 4 + int(thoughtfulness_ * 12 / 100); // e.g. t=70 -> 12

        // Iterative deepening on the main thread
        for (int depth = 1; depth <= max_depth; ++depth)
        {
            int prev_best_move_changes = best_move_changes; // snapshot for per-depth stability
            trace_search("depth-start depth=" + std::to_string(depth));
            main_td.seldepth = 0;
            main_td.root_depth = depth;
            main_td.diag.clear(); // Reset per-depth diagnostic counters

            // Emit root static eval breakdown at depth 1
            if (depth == 1 && diag_enabled_)
            {
                int nnue_score = NNUE::nnue_evaluate(board);
                int hce_score = evaluate(board);
                int corr_idx = int(board.pawn_key() % ThreadData::CORR_HIST_SIZE);
                int correction = main_td.pawn_correction[corr_idx] / ThreadData::CORR_GRAIN;
                std::cout << "info string static_eval"
                          << " nnue " << nnue_score
                          << " hce " << hce_score
                          << " correction " << correction
                          << " adjusted_nnue " << (nnue_score + correction)
                          << " nnue_loaded " << (NNUE::nnue_is_loaded() ? 1 : 0)
                          << std::endl;

                // Child position evaluations: for every legal root move, make it,
                // evaluate both NNUE and HCE from the parent's (STM) perspective,
                // then undo. Reveals per-move NNUE bias vs classical eval.
                // Capped at 20 moves and guarded by should_stop so this diagnostic
                // never burns the entire search budget (e.g. 140+ queens-only pos).
                MoveList root_legal;
                generate_legal(board, root_legal);
                int ce_limit = std::min(root_legal.size(), 20);
                for (int i = 0; i < ce_limit; ++i)
                {
                    if (should_stop(main_td))
                        break;
                    Move m = root_legal[i].move;
                    StateInfo ce_state;
                    Board ce_board = board; // copy: board is always the root
                    ce_board.do_move(m, ce_state);

                    // Values are from child's STM perspective â†’ negate for parent's view
                    int ce_nnue = -NNUE::nnue_evaluate(ce_board);
                    int ce_hce = -evaluate(ce_board);
                    int ce_mat = -evaluate_material(ce_board);
                    int ce_corr_idx = int(ce_board.pawn_key() % ThreadData::CORR_HIST_SIZE);
                    int ce_corr = main_td.pawn_correction[ce_corr_idx] / ThreadData::CORR_GRAIN;

                    std::cout << "info string childeval"
                              << " move " << m.to_uci()
                              << " nnue " << ce_nnue
                              << " hce " << ce_hce
                              << " mat " << ce_mat
                              << " corr " << ce_corr
                              << " diff " << (ce_nnue - ce_hce)
                              << std::endl;
                }
            }

            // Aspiration windows for depths > 4
            int alpha = -VALUE_INFINITE;
            int beta = VALUE_INFINITE;
            int delta = SP.asp_delta;

            if (depth >= SP.asp_min_depth)
            {
                alpha = std::max(best_score - delta, -int(VALUE_INFINITE));
                beta = std::min(best_score + delta, int(VALUE_INFINITE));
            }

            int score;
            int asp_iteration = 0;
            while (true)
            {
                if (use_root_split)
                {
                    // --- ROOT_SPLIT path: dispatch root moves to thread pool ---
                    // Generate and score root moves
                    MoveList root_moves;
                    generate_legal(board, root_moves);
                    // Get TT move for scoring
                    TTEntry tt_entry;
                    Move tt_move = Move::none();
                    if (TT.probe(board.key(), tt_entry) && tt_entry.move)
                        tt_move = tt_entry.move;
                    score_moves(main_td, root_moves, tt_move, 0);
                    // Sort root moves by score (descending) so PV move is index 0
                    std::sort(root_moves.begin(), root_moves.end(),
                              [](const ScoredMove &a, const ScoredMove &b)
                              {
                                  return a.score > b.score;
                              });

                    // Set up shared state for workers
                    rs_board_ = board;
                    rs_root_moves_ = root_moves;
                    rs_depth_ = depth;
                    // Use previous depth's score as starting alpha (helps workers
                    // prune bad moves faster).  Fall back to -INF at depth 1.
                    int start_alpha = (depth >= 2 && best_score > -VALUE_INFINITE)
                                          ? best_score - 100 // 100cp window below previous best
                                          : -VALUE_INFINITE;
                    rs_best_score_.store(start_alpha, std::memory_order_relaxed);
                    rs_best_move_ = Move::none();

                    // Wake helper threads
                    {
                        std::lock_guard<std::mutex> lk(pool_mutex_);
                        pool_done_count_ = 0;
                        pool_go_ = true;
                    }
                    pool_cv_.notify_all();

                    // Main thread participates as worker 0
                    root_split_search(main_td, board, root_moves, depth, 0, num_threads_);

                    // Wait for all helpers to finish
                    {
                        std::unique_lock<std::mutex> lk(pool_mutex_);
                        pool_done_cv_.wait(lk, [this]
                                           { return pool_done_count_ >= num_threads_ - 1; });
                        pool_go_ = false; // reset for next iteration
                    }

                    score = rs_best_score_.load(std::memory_order_relaxed);
                    // If no move improved alpha, use the PV move with alpha as score
                    if (!rs_best_move_)
                    {
                        // Fallback: search PV move with full window on main thread
                        score = alpha_beta(main_td, depth, -VALUE_INFINITE, VALUE_INFINITE, 0, true);
                    }
                }
                else
                {
                    // --- LAZY_SMP path: main thread does full search ---
                    score = alpha_beta(main_td, depth, alpha, beta, 0, true);
                }

                if (should_stop(main_td))
                    break;

                // ROOT_SPLIT does not use aspiration re-search (workers already
                // searched with their own alpha bounds), so break immediately.
                if (use_root_split)
                    break;

                // Gradual aspiration widening (LAZY_SMP only):
                // Double delta on each fail until delta > 900, then open to ±∞.
                // This avoids both the expense of immediate full-window re-searches
                // and the risk of too many narrow re-tries.
                if (score <= alpha)
                {
                    if (alpha <= -int(VALUE_INFINITE))
                        break;
                    ++asp_iteration;
                    if (info_enabled_)
                        std::cout << "info string aspiration fail-low depth " << depth
                                  << " iter " << asp_iteration
                                  << " delta " << delta
                                  << " alpha " << alpha
                                  << " beta " << beta
                                  << " score " << score << std::endl;
                    beta = (alpha + beta) / 2;
                    delta *= 2;
                    alpha = (delta > 900)
                                ? -int(VALUE_INFINITE)
                                : std::max(best_score - delta, -int(VALUE_INFINITE));
                }
                else if (score >= beta)
                {
                    if (beta >= int(VALUE_INFINITE))
                        break;
                    ++asp_iteration;
                    if (info_enabled_)
                        std::cout << "info string aspiration fail-high depth " << depth
                                  << " iter " << asp_iteration
                                  << " delta " << delta
                                  << " alpha " << alpha
                                  << " beta " << beta
                                  << " score " << score << std::endl;
                    delta *= 2;
                    beta = (delta > 900)
                               ? int(VALUE_INFINITE)
                               : std::min(best_score + delta, int(VALUE_INFINITE));
                }
                else
                {
                    break;
                }
            }

            if (should_stop(main_td) && depth > 1)
                break;

            // Extract best move
            if (use_root_split && rs_best_move_)
            {
                // ROOT_SPLIT: best move tracked explicitly by workers
                if (best_move && rs_best_move_ != best_move)
                    best_move_changes++;
                best_move = rs_best_move_;
                best_score = score;
                // Store into TT so PV extraction and rootmove table work correctly
                TT.store(board.key(), best_move, score, depth, TT_EXACT);
            }
            else
            {
                // LAZY_SMP: extract from TT
                TTEntry entry;
                if (TT.probe(board.key(), entry) && entry.move)
                {
                    if (best_move && entry.move != best_move)
                        best_move_changes++;
                    best_move = entry.move;
                    best_score = score;
                }
            }

            // Update stats
            main_stats_.depth = depth;
            if (use_root_split)
            {
                // Aggregate seldepth from all worker threads
                int max_seldepth = main_td.seldepth;
                for (int i = 1; i < num_threads_; ++i)
                    max_seldepth = std::max(max_seldepth, thread_data_[i]->seldepth);
                main_stats_.seldepth = max_seldepth;
            }
            else
            {
                main_stats_.seldepth = main_td.seldepth;
            }
            main_stats_.score = score;
            main_stats_.bestmove = best_move;

            // Collect total nodes from all threads
            int64_t total_nodes = main_td.nodes;
            for (int i = 1; i < num_threads_; ++i)
                total_nodes += thread_data_[i]->nodes;
            main_stats_.nodes = total_nodes;

            // Build PV from TT
            std::string pv;
            Board pv_board = board;
            StateInfo pv_states[MAX_PLY];
            for (int i = 0; i < depth; ++i)
            {
                TTEntry pv_entry;
                if (!TT.probe(pv_board.key(), pv_entry) || !pv_entry.move)
                    break;
                if (!pv.empty())
                    pv += " ";
                pv += pv_entry.move.to_uci();
                pv_board.do_move(pv_entry.move, pv_states[i]);
            }

            // Print info
            print_info(main_stats_, pv);
            trace_search("depth-done depth=" + std::to_string(depth) + " nodes=" + std::to_string(main_stats_.nodes) + " score=" + std::to_string(main_stats_.score) + " bestmove=" + (best_move ? best_move.to_uci() : std::string("0000")));

            // Root move score table: probe TT for every root move to report scores
            if (diag_enabled_)
            {
                MoveList root_legal;
                generate_legal(board, root_legal);
                std::ostringstream rms;
                rms << "info string rootmoves depth " << depth;
                StateInfo rm_state;
                for (int i = 0; i < root_legal.size(); ++i)
                {
                    Move rm = root_legal[i].move;
                    Board rm_board = board;
                    rm_board.do_move(rm, rm_state);
                    TTEntry rm_entry;
                    bool rm_hit = TT.probe(rm_board.key(), rm_entry);
                    // Negate score: TT stores from child's perspective
                    int rm_score = rm_hit ? -rm_entry.score : -99999;
                    int rm_depth = rm_hit ? rm_entry.depth : 0;
                    char rm_flag = rm_hit ? (rm_entry.flag == TT_EXACT  ? 'E'
                                             : rm_entry.flag == TT_BETA ? 'B'
                                                                        : 'A')
                                          : '?';
                    rms << " " << rm.to_uci() << ":" << rm_score
                        << "/" << rm_flag << rm_depth;
                }
                std::cout << rms.str() << std::endl;
            }

            // Pruning / reduction diagnostics for this depth
            if (diag_enabled_)
            {
                auto &d = main_td.diag;
                std::cout << "info string searchdiag depth " << depth
                          << " tt_cuts " << d.tt_cutoffs
                          << " null_cuts " << d.null_move_cuts
                          << " rfp " << d.rfp_cuts
                          << " razoring " << d.razoring_cuts
                          << " probcut " << d.probcut_cuts
                          << " futility " << d.futility_prunes
                          << " lmp " << d.lmp_prunes
                          << " see " << d.see_prunes
                          << " hist " << d.hist_prunes
                          << " lmr " << d.lmr_searches
                          << " lmr_re " << d.lmr_re_searches
                          << std::endl;
            }

            // Stop immediately if we've proven a forced mate â€” no deeper search needed
            if (std::abs(score) >= VALUE_MATE_IN_MAX_PLY)
                break;

            // Confidence tracking: record eval and PV stability for early-exit gate
            conf_eval_hist[conf_eval_idx % CONF_HIST] = double(score);
            conf_eval_idx++;
            conf_total_depths++;
            if (best_move_changes == prev_best_move_changes)
                conf_stable_depths++;

            // Confidence early exit: stop before starting a new depth when
            // the engine is clearly stable about its choice.
            // Disabled in movetime mode -- movetime is a hard deadline; use the full budget.
            if (limits.movetime == 0 && soft_time_ms_ > 0 && depth >= min_conf_depth && conf_total_depths >= 3)
            {
                int64_t elapsed = main_stats_.elapsed_ms();
                if (elapsed >= soft_time_ms_ * 15 / 100)
                {
                    int n = std::min(conf_total_depths, CONF_HIST);
                    double sum = 0.0, sq = 0.0;
                    for (int i = 0; i < n; i++)
                    {
                        int idx = ((conf_eval_idx - n + i) % CONF_HIST + CONF_HIST) % CONF_HIST;
                        sum += conf_eval_hist[idx];
                        sq += conf_eval_hist[idx] * conf_eval_hist[idx];
                    }
                    double mean = sum / n;
                    double var = std::max(0.0, sq / n - mean * mean);
                    double eval_stability = 1.0 / (1.0 + std::sqrt(var) / 50.0);
                    double pv_support = double(conf_stable_depths) / conf_total_depths;
                    double complex_factor = std::min(1.0, conf_total_depths / 12.0);
                    double confidence = std::sqrt(eval_stability * pv_support) * complex_factor;
                    if (confidence >= conf_threshold)
                    {
                        if (info_enabled_)
                            std::cout << "info string confidence_exit depth " << depth
                                      << " conf " << int(confidence * 1000)
                                      << " threshold " << int(conf_threshold * 1000)
                                      << " elapsed " << elapsed << "ms" << std::endl;
                        break;
                    }
                }
            }

            // Time management: check soft limit with instability + fallingEval.
            // Disabled in movetime mode -- hard limit in should_stop() handles the deadline.
            if (limits.movetime == 0 && soft_time_ms_ > 0)
            {
                int64_t elapsed = main_stats_.elapsed_ms();
                // Extend time if best move is unstable (changing frequently)
                double instability = 1.0 + 0.5 * std::min(best_move_changes, 4);
                // FallingEval: extend time when score is declining between iterations
                double falling_eval = 1.0;
                if (depth >= 5 && prev_depth_score > -VALUE_INFINITE)
                {
                    // Score drop → extend; score rise → contract
                    falling_eval = std::clamp(1.0 + (prev_depth_score - score) / 200.0, 0.5, 1.5);
                }
                int64_t adjusted_soft = int64_t(soft_time_ms_ * instability * falling_eval);
                // Decay instability after a few stable iterations
                if (best_move_changes > 0 && depth > 6)
                    best_move_changes = std::max(0, best_move_changes - 1);
                if (elapsed > adjusted_soft)
                    break; // Don't start a new depth
            }
            prev_depth_score = score;
        }

        // In infinite / ponder mode, the engine must wait for an explicit 'stop'
        // command rather than returning bestmove when the ID loop exhausts all
        // depths or finds a forced mate.  Spin-wait with low CPU usage.
        // NOTE: We check pondering_ (not limits.ponder) so that ponderhit can
        // clear the flag and let us fall through to emit bestmove with the
        // time-limited result.  limits.ponder is a snapshot of the original
        // 'go' command and never changes.
        if ((limits.infinite || pondering_.load(std::memory_order_relaxed)) && !stop_flag_.load(std::memory_order_relaxed))
        {
            while (!stop_flag_.load(std::memory_order_relaxed) && (limits.infinite || pondering_.load(std::memory_order_relaxed)))
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }

        stop_flag_.store(true);

        // Join lazy SMP helper threads (only used in LAZY_SMP mode)
        if (!use_root_split)
        {
            for (auto &t : threads_)
            {
                if (t.joinable())
                    t.join();
            }
            threads_.clear();
        }
        // ROOT_SPLIT: pool threads stay alive between searches; stop_flag
        // causes them to exit their current root_split_search loop.

        // Print a final info line with the true total elapsed time and node count.
        // The last per-iteration info line reflects the moment that depth completed,
        // not the actual end-of-search time.  This line gives GUIs/profilers the
        // real figures (total nodes including any partial last iteration).
        {
            int64_t total_nodes = main_td.nodes;
            for (int i = 1; i < num_threads_; ++i)
                total_nodes += thread_data_[i]->nodes;
            main_stats_.nodes = total_nodes;
            std::string final_pv;
            Board pv_board = board;
            StateInfo pv_states[MAX_PLY];
            for (int i = 0; i < main_stats_.depth; ++i)
            {
                TTEntry pv_entry;
                if (!TT.probe(pv_board.key(), pv_entry) || !pv_entry.move)
                    break;
                if (!final_pv.empty())
                    final_pv += " ";
                final_pv += pv_entry.move.to_uci();
                pv_board.do_move(pv_entry.move, pv_states[i]);
            }
            print_info(main_stats_, final_pv);
            if (profile_enabled())
                finalize_profile(total_nodes);
        }

        searching_.store(false);
        if (profile_enabled())
            dump_profile_stderr();

        // Safety fallback: if the search was interrupted before any best move was
        // established (e.g. a timeout fired during depth-1 diagnostics before
        // alpha_beta could run), return the first legal move rather than (none).
        // This prevents the bot from crashing on exotic high-branching positions.
        if (!best_move)
        {
            MoveList fallback_legal;
            generate_legal(board, fallback_legal);
            if (fallback_legal.size() > 0)
                best_move = fallback_legal[0].move;
        }

        trace_search("exit bestmove=" + (best_move ? best_move.to_uci() : std::string("0000")) + " nodes=" + std::to_string(main_stats_.nodes));

        // Extract ponder move
        if (best_move)
        {
            StateInfo st;
            Board temp = board;
            temp.do_move(best_move, st);
            TTEntry ponder_entry;
            if (TT.probe(temp.key(), ponder_entry) && ponder_entry.move)
                main_stats_.ponder_move = ponder_entry.move;
        }

        return best_move;
    }

    void SearchEngine::stop()
    {
        stop_flag_.store(true);
    }

    void SearchEngine::print_info(const SearchStats &stats, const std::string &pv)
    {
        int64_t elapsed = stats.elapsed_ms();
        int64_t nps = stats.nodes * 1000 / std::max(elapsed, int64_t(1));

        std::ostringstream ss;
        ss << "info depth " << stats.depth
           << " seldepth " << stats.seldepth
           << " score ";

        if (std::abs(stats.score) >= VALUE_MATE_IN_MAX_PLY)
        {
            int mate_in = (VALUE_MATE - std::abs(stats.score) + 1) / 2;
            if (stats.score < 0)
                mate_in = -mate_in;
            ss << "mate " << mate_in;
        }
        else
        {
            ss << "cp " << stats.score;
        }

        ss << " nodes " << stats.nodes
           << " nps " << nps
           << " time " << elapsed
           << " hashfull " << TT.hashfull();

        if (!pv.empty())
            ss << " pv " << pv;

        std::string info = ss.str();
        if (info_enabled_)
            std::cout << info << std::endl;

        if (info_callback_)
            info_callback_(stats, pv);
    }

    void SearchEngine::finalize_profile(int64_t total_nodes)
    {
        last_profile_ = SearchProfile{};
        last_profile_.threads = num_threads_;
        last_profile_.nodes = total_nodes;
        last_smp_ = SearchSmpStats{};
        last_smp_.threads = num_threads_;
#ifdef PROFILE
        last_profile_.cycle_counters_enabled = true;
#endif
        for (int i = 0; i < num_threads_; ++i)
        {
            const auto &td = *thread_data_[i];
            last_profile_.diag += td.diag;
#ifdef PROFILE
            last_profile_.cycles_nnue += td.cycles_nnue;
            last_profile_.cycles_movegen += td.cycles_movegen;
            last_profile_.cycles_do_move += td.cycles_do_move;
            last_profile_.cycles_undo_move += td.cycles_undo_move;
            last_profile_.cycles_see += td.cycles_see;
            last_profile_.cycles_gcheck += td.cycles_gcheck;
#endif
        }
        last_profile_.cycles_total = last_profile_.cycles_nnue + last_profile_.cycles_movegen + last_profile_.cycles_do_move + last_profile_.cycles_undo_move + last_profile_.cycles_see + last_profile_.cycles_gcheck;
        last_profile_.nnue = NNUE::nnue_profile_snapshot();
    }

    void SearchEngine::dump_profile_stderr() const
    {
        const auto &p = last_profile_;
        if (!p.cycle_counters_enabled || p.cycles_total == 0 || p.nodes <= 0)
            return;
        auto pct = [&](uint64_t c)
        { return int(100 * c / std::max<uint64_t>(1, p.cycles_total)); };
        std::cerr << "PROFILE nodes=" << p.nodes
                  << " ab=" << p.diag.alpha_beta_nodes
                  << " q=" << p.diag.quiescence_nodes
                  << " tt=" << p.diag.tt_hits << "/" << p.diag.tt_probes
                  << " nnue=" << pct(p.cycles_nnue) << "%"
                  << " movegen=" << pct(p.cycles_movegen) << "%"
                  << " do_move=" << pct(p.cycles_do_move) << "%"
                  << " undo_move=" << pct(p.cycles_undo_move) << "%"
                  << " see=" << pct(p.cycles_see) << "%"
                  << " gcheck=" << pct(p.cycles_gcheck) << "%"
                  << " cycles/node=" << (p.cycles_total / std::max<int64_t>(1, p.nodes))
                  << " | nnue " << NNUE::nnue_refresh_stats()
                  << " forward=" << p.nnue.forward_calls
                  << " updates=" << p.nnue.incremental_updates
                  << " captures=" << p.nnue.capture_updates
                  << " full_updates=" << p.nnue.full_updates
                  << " null_updates=" << p.nnue.null_updates
                  << " refresh_cycles=" << p.nnue.cycles_refresh
                  << " forward_cycles=" << p.nnue.cycles_forward
                  << " update_cycles=" << p.nnue.cycles_update
                  << "\n";
    }

} // namespace Chess
