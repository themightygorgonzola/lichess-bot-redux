#include "uci.h"
#include "benchmark.h"
#include "eval.h"
#include "eval_params.h"
#include "movegen.h"
#include "nnue/nnue_eval.h"
#include "syzygy/syzygy.h"
#include <iomanip>
#include <iostream>
#include <sstream>
#include <thread>
#include <chrono>

namespace Chess
{

    UCI::UCI()
    {
        board_.set_startpos();
    }

    void UCI::loop()
    {
        std::string line;

        while (running_ && std::getline(std::cin, line))
        {
            std::istringstream is(line);
            std::string cmd;
            is >> cmd;

            if (cmd == "uci")
                cmd_uci();
            else if (cmd == "isready")
                cmd_isready();
            else if (cmd == "ucinewgame")
                cmd_ucinewgame();
            else if (cmd == "position")
                cmd_position(is);
            else if (cmd == "go")
                cmd_go(is);
            else if (cmd == "stop")
                cmd_stop();
            else if (cmd == "ponderhit")
                cmd_ponderhit(is);
            else if (cmd == "quit")
                cmd_quit();
            else if (cmd == "setoption")
                cmd_setoption(is);
            else if (cmd == "d")
                cmd_display();
            else if (cmd == "eval")
                cmd_eval();
            else if (cmd == "evalvec")
                cmd_evalvec();
            else if (cmd == "perft")
                cmd_perft(is);
            else if (cmd == "bench")
                cmd_bench(is);
            else if (cmd == "exit")
                cmd_quit();
        }
        // EOF on stdin — treat as quit (ensures pool threads are shut down cleanly)
        if (running_)
            cmd_quit();
    }

    void UCI::cmd_uci()
    {
        std::cout << "id name Redux 1.0.0" << std::endl;
        std::cout << "id author Redux Team" << std::endl;
        std::cout << std::endl;
        std::cout << "option name Hash type spin default 64 min 1 max 4096" << std::endl;
        std::cout << "option name Threads type spin default 1 min 1 max 256" << std::endl;
#ifndef DISABLE_NNUE
        std::cout << "option name EvalFile type string default <empty>" << std::endl;
        std::cout << "option name UseNNUE type check default true" << std::endl;
#endif
        std::cout << "option name SearchType type combo default LazySMP var LazySMP var RootSplit" << std::endl;
        std::cout << "option name SyzygyPath type string default <empty>" << std::endl;
        std::cout << "option name Thoughtfulness type spin default 70 min 0 max 100" << std::endl;
        std::cout << "option name SyzygyProbeDepth type spin default 1 min 0 max 100" << std::endl;
        std::cout << "option name Syzygy50MoveRule type check default true" << std::endl;
        std::cout << eval_options_uci_string();
        // Search tuning options — defaults read directly from SP so search.h is
        // the single source of truth. No need to keep this in sync manually.
        std::cout << "option name LmrBase type string default "
                  << std::fixed << std::setprecision(2) << SP.lmr_base << "\n";
        std::cout << "option name LmrDivisor type string default "
                  << std::fixed << std::setprecision(2) << SP.lmr_divisor << "\n";
        std::cout << "option name LmrHistDiv type spin default " << SP.lmr_hist_div << " min 1000 max 20000\n";
        std::cout << "option name RfpMargin type spin default " << SP.rfp_margin << " min 20 max 200\n";
        std::cout << "option name RfpImprovingSub type spin default " << SP.rfp_improving_sub << " min 0 max 100\n";
        std::cout << "option name NmpBaseR type spin default " << SP.nmp_base_r << " min 1 max 6\n";
        std::cout << "option name NmpDepthDiv type spin default " << SP.nmp_depth_div << " min 2 max 8\n";
        std::cout << "option name NmpEvalDiv type spin default " << SP.nmp_eval_div << " min 50 max 500\n";
        std::cout << "option name FpBase type spin default " << SP.fp_base << " min 0 max 300\n";
        std::cout << "option name FpDepthScale type spin default " << SP.fp_depth_scale << " min 30 max 200\n";
        std::cout << "option name SeeQuietScale type spin default " << SP.see_quiet_scale << " min 5 max 60\n";
        std::cout << "option name SeeCaptScale type spin default " << SP.see_capt_scale << " min 50 max 300\n";
        std::cout << "option name HistPruneScale type spin default " << SP.hist_prune_scale << " min 500 max 8000\n";
        std::cout << "option name AspDelta type spin default " << SP.asp_delta << " min 10 max 150\n";
        std::cout << "option name DiagMode type check default false" << std::endl;
        std::cout << std::endl;
        std::cout << "uciok" << std::endl;
    }

    void UCI::cmd_isready()
    {
        std::cout << "readyok" << std::endl;
    }

    void UCI::cmd_ucinewgame()
    {
        TT.clear();
        Search.clear_all(); // Reset all learned history tables
        board_.set_startpos();
    }

    void UCI::cmd_position(std::istringstream &is)
    {
        std::string token;
        is >> token;

        if (token == "startpos")
        {
            board_.set_startpos();
            is >> token; // Consume "moves" if present
        }
        else if (token == "fen")
        {
            std::string fen;
            while (is >> token && token != "moves")
            {
                if (!fen.empty())
                    fen += ' ';
                fen += token;
            }
            board_.set_fen(fen);
        }

        // Apply moves
        static StateInfo state_storage[512]; // Persistent storage
        int state_idx = 0;

        while (is >> token)
        {
            if (token == "moves")
                continue;

            // Find the matching legal move
            MoveList legal;
            generate_legal(board_, legal);

            Move move = Move::none();
            for (int i = 0; i < legal.size(); ++i)
            {
                if (legal[i].move.to_uci() == token)
                {
                    move = legal[i].move;
                    break;
                }
            }

            // If not found in legal moves (e.g. unusual/custom position), apply it anyway
            // so the internal board stays in sync with the game record.
            if (!move)
            {
                Move raw = Move::from_uci(token);
                if (raw && is_ok(raw.from()) && is_ok(raw.to()))
                {
                    Piece pc = board_.piece_on(raw.from());
                    if (pc != NO_PIECE)
                    {
                        // Upgrade move type for en passant / castling if applicable
                        if (raw.type() != PROMOTION)
                        {
                            if (type_of(pc) == PAWN && file_of(raw.from()) != file_of(raw.to()) && board_.piece_on(raw.to()) == NO_PIECE && board_.ep_square() == raw.to())
                            {
                                raw = Move(raw.from(), raw.to(), EN_PASSANT);
                            }
                            else if (type_of(pc) == KING)
                            {
                                bool c960 = board_.piece_on(raw.to()) == make_piece(color_of(pc), ROOK);
                                bool std2 = std::abs((int)file_of(raw.to()) - (int)file_of(raw.from())) == 2 && rank_of(raw.from()) == rank_of(raw.to());
                                if (c960 || std2)
                                    raw = Move(raw.from(), raw.to(), CASTLING);
                            }
                        }
                        move = raw;
                    }
                    else
                    {
                        // From-square is empty: use a null move to advance the turn only.
                        board_.do_null_move(state_storage[state_idx++]);
                        if (state_idx >= 510)
                            state_idx = 0;
                        continue;
                    }
                }
            }

            if (move)
            {
                board_.do_move(move, state_storage[state_idx++]);
                if (state_idx >= 510)
                    state_idx = 0; // Wrap (shouldn't happen)
            }
        }
    }

    void UCI::wait_for_search()
    {
        if (search_thread_.joinable())
            search_thread_.join();
    }

    void UCI::cmd_go(std::istringstream &is)
    {
        SearchLimits limits;
        std::string token;

        while (is >> token)
        {
            if (token == "depth")
                is >> limits.depth;
            else if (token == "nodes")
                is >> limits.nodes;
            else if (token == "movetime")
                is >> limits.movetime;
            else if (token == "wtime")
                is >> limits.wtime;
            else if (token == "btime")
                is >> limits.btime;
            else if (token == "winc")
                is >> limits.winc;
            else if (token == "binc")
                is >> limits.binc;
            else if (token == "movestogo")
                is >> limits.movestogo;
            else if (token == "opptime")
                is >> limits.opp_time;
            else if (token == "infinite")
                limits.infinite = true;
            else if (token == "ponder")
                limits.ponder = true;
            else if (token == "perft")
            {
                int depth;
                is >> depth;
                cmd_perft(is);
                return;
            }
        }

        // Wait for any previous search to fully complete
        wait_for_search();

        // Run search in a separate thread so UCI can still receive "stop"
        search_thread_ = std::thread([this, limits]()
                                     {
        Board board_copy = board_;
        Move best = Search.search(board_copy, limits);

        // Emit bestmove with optional ponder move
        auto stats = Search.get_stats();
        std::cout << "bestmove " << best.to_uci();
        if (stats.ponder_move)
            std::cout << " ponder " << stats.ponder_move.to_uci();
        std::cout << std::endl; });
    }

    void UCI::cmd_stop()
    {
        Search.stop();
        wait_for_search();
    }

    void UCI::cmd_ponderhit(std::istringstream &is)
    {
        // Parse optional clock arguments: ponderhit wtime X btime Y winc Z binc Z
        // If absent (standard UCI), passing zeros triggers the saved-limits fallback.
        int wt = 0, bt = 0, wi = 0, bi = 0;
        std::string tok;
        while (is >> tok)
        {
            if (tok == "wtime")
                is >> wt;
            else if (tok == "btime")
                is >> bt;
            else if (tok == "winc")
                is >> wi;
            else if (tok == "binc")
                is >> bi;
        }
        Search.ponderhit(wt, bt, wi, bi, 0);
    }

    void UCI::cmd_quit()
    {
        Search.stop();
        wait_for_search();
        running_ = false;
    }

    void UCI::cmd_setoption(std::istringstream &is)
    {
        std::string token, name, value;

        // Parse "name <name> value <value>"
        is >> token; // "name"
        while (is >> token && token != "value")
        {
            if (!name.empty())
                name += ' ';
            name += token;
        }
        while (is >> token)
        {
            if (!value.empty())
                value += ' ';
            value += token;
        }

        // Apply options
        if (name == "Hash")
        {
            int mb = std::stoi(value);
            TT.resize(mb);
        }
        else if (name == "Threads")
        {
            int n = std::stoi(value);
            Search.set_threads(n);
        }
        else if (name == "SearchType")
        {
            if (value == "RootSplit")
                Search.set_mode(ROOT_SPLIT);
            else
                Search.set_mode(LAZY_SMP);
        }
#ifndef DISABLE_NNUE
        else if (name == "EvalFile")
        {
            if (!value.empty() && value != "<empty>")
            {
                NNUE::nnue_init(value);
            }
        }
        else if (name == "UseNNUE")
        {
            NNUE::nnue_set_enabled(value == "true");
        }
#endif
        else if (name == "Thoughtfulness")
        {
            try
            {
                Search.set_thoughtfulness(std::stoi(value));
            }
            catch (...)
            {
            }
        }
        else if (name == "SyzygyPath")
        {
            syzygy_init(value);
        }
        else if (name == "SyzygyProbeDepth")
        {
            try
            {
                syzygy_set_probe_depth(std::stoi(value));
            }
            catch (...)
            {
            }
        }
        else if (name == "Syzygy50MoveRule")
        {
            syzygy_set_50move_rule(value == "true");
        }
        else if (name.substr(0, 4) == "Eval")
        {
            // Eval parameter: parse as integer and dispatch
            try
            {
                int v = std::stoi(value);
                if (!set_eval_param_by_name(name, v))
                {
                    std::cerr << "info string Unknown eval option: " << name << std::endl;
                }
            }
            catch (...)
            {
                std::cerr << "info string Invalid value for " << name << ": " << value << std::endl;
            }
        }
        // Search tuning options
        else if (name == "LmrBase")
        {
            try
            {
                SP.lmr_base = std::stod(value);
                Search.rebuild_lmr();
            }
            catch (...)
            {
            }
        }
        else if (name == "LmrDivisor")
        {
            try
            {
                SP.lmr_divisor = std::stod(value);
                Search.rebuild_lmr();
            }
            catch (...)
            {
            }
        }
        else if (name == "LmrHistDiv")
        {
            try
            {
                SP.lmr_hist_div = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "RfpMargin")
        {
            try
            {
                SP.rfp_margin = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "RfpImprovingSub")
        {
            try
            {
                SP.rfp_improving_sub = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "NmpBaseR")
        {
            try
            {
                SP.nmp_base_r = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "NmpDepthDiv")
        {
            try
            {
                SP.nmp_depth_div = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "NmpEvalDiv")
        {
            try
            {
                SP.nmp_eval_div = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "FpBase")
        {
            try
            {
                SP.fp_base = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "FpDepthScale")
        {
            try
            {
                SP.fp_depth_scale = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "SeeQuietScale")
        {
            try
            {
                SP.see_quiet_scale = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "SeeCaptScale")
        {
            try
            {
                SP.see_capt_scale = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "HistPruneScale")
        {
            try
            {
                SP.hist_prune_scale = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "AspDelta")
        {
            try
            {
                SP.asp_delta = std::stoi(value);
            }
            catch (...)
            {
            }
        }
        else if (name == "DiagMode")
        {
            Search.set_diag_enabled(value == "true");
        }
    }

    void UCI::cmd_display()
    {
        board_.print();

        MoveList legal;
        generate_legal(board_, legal);
        std::cout << "Legal moves (" << legal.size() << "): ";
        for (int i = 0; i < legal.size(); ++i)
        {
            std::cout << legal[i].move.to_uci() << " ";
        }
        std::cout << std::endl;
    }

    void UCI::cmd_eval()
    {
        std::cout << "\nEval breakdown for current position:\n";
        int score = evaluate_explain(board_);
        std::cout << "\nFinal score (side-to-move perspective): " << score << " cp\n"
                  << std::endl;
    }

    void UCI::cmd_evalvec()
    {
        EvalTrace tr = evaluate_trace(board_);

        // Build a single-line JSON object with all term MG/EG pairs,
        // plus phase, totals, and final score.
        // Format: evalvec {"material_pst":[mg,eg],...,"phase":N,"mg":N,"eg":N,"total":N,"stm":N}
        std::string json = "{";
        for (int i = 0; i < EVAL_TERM_COUNT; ++i)
        {
            if (i > 0)
                json += ',';
            char buf[64];
            std::snprintf(buf, sizeof(buf), "\"%s\":[%d,%d]",
                          EVAL_TERM_KEYS[i], tr.terms[i].mg, tr.terms[i].eg);
            json += buf;
        }
        {
            char buf[128];
            std::snprintf(buf, sizeof(buf),
                          ",\"phase\":%d,\"mg\":%d,\"eg\":%d,\"total\":%d,\"stm\":%d,\"scale_factor\":%d",
                          tr.phase, tr.mg_total, tr.eg_total, tr.blended, tr.stm_score, tr.scale_factor);
            json += buf;
        }
        json += '}';
        std::cout << "evalvec " << json << std::endl;
    }

    void UCI::cmd_perft(std::istringstream &is)
    {
        int depth = 5;
        is >> depth;

        std::cout << "\nPerft results:" << std::endl;
        auto start = std::chrono::steady_clock::now();

        MoveList legal;
        generate_legal(board_, legal);

        uint64_t total = 0;
        for (int i = 0; i < legal.size(); ++i)
        {
            StateInfo state;
            board_.do_move(legal[i].move, state);
            uint64_t count = perft(board_, depth - 1);
            board_.undo_move(legal[i].move);
            total += count;
            std::cout << legal[i].move.to_uci() << ": " << count << std::endl;
        }

        auto elapsed = std::chrono::steady_clock::now() - start;
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count();

        std::cout << "\nNodes: " << total << std::endl;
        std::cout << "Time:  " << ms << " ms" << std::endl;
        if (ms > 0)
            std::cout << "NPS:   " << (total * 1000 / ms) << std::endl;
    }

    void UCI::cmd_bench(std::istringstream &is)
    {
        using namespace Benchmark;

        SearchBenchConfig cfg;
        cfg.depth = 12;
        cfg.threads = 1;
        cfg.hash_mb = 64;
        cfg.warmup_depth = 5;
        cfg.repeat = 1;
        cfg.clear_tt_each_position = true;
        cfg.clear_history_each_position = true;
        cfg.collect_profile = false;

        // Parse optional args: bench [depth N] [threads N] [hash N] [json]
        bool json_out = false;
        std::string tok;
        while (is >> tok)
        {
            if (tok == "depth")
                is >> cfg.depth;
            else if (tok == "threads")
            {
                is >> cfg.threads;
            }
            else if (tok == "hash")
                is >> cfg.hash_mb;
            else if (tok == "json")
                json_out = true;
        }

        wait_for_search();

        auto suite = default_search_suite();
        auto summary = run_search_benchmark(suite, cfg);

        if (json_out)
            print_search_benchmark_json(std::cout, summary);
        else
            print_search_benchmark_text(std::cout, summary);
    }

} // namespace Chess
