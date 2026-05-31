#include "benchmark.h"

#include "board.h"
#include "tt.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <sstream>
#include <stdexcept>

namespace Chess::Benchmark
{

    namespace
    {

        bool bench_trace_enabled()
        {
            static const bool enabled = []()
            {
                const char *v = std::getenv("REDUX_TRACE_BENCH");
                if (!v || !*v)
                    return false;
                std::string s(v);
                std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c)
                               { return static_cast<char>(std::tolower(c)); });
                return s != "0" && s != "false" && s != "off";
            }();
            return enabled;
        }

        std::string trim(const std::string &s)
        {
            const auto first = s.find_first_not_of(" \t\r\n");
            if (first == std::string::npos)
                return {};
            const auto last = s.find_last_not_of(" \t\r\n");
            return s.substr(first, last - first + 1);
        }

        std::string json_escape(const std::string &s)
        {
            std::ostringstream os;
            for (unsigned char c : s)
            {
                switch (c)
                {
                case '\\':
                    os << "\\\\";
                    break;
                case '"':
                    os << "\\\"";
                    break;
                case '\b':
                    os << "\\b";
                    break;
                case '\f':
                    os << "\\f";
                    break;
                case '\n':
                    os << "\\n";
                    break;
                case '\r':
                    os << "\\r";
                    break;
                case '\t':
                    os << "\\t";
                    break;
                default:
                    if (c < 0x20)
                    {
                        os << "\\u"
                           << std::hex << std::setw(4) << std::setfill('0') << int(c)
                           << std::dec << std::setfill(' ');
                    }
                    else
                    {
                        os << static_cast<char>(c);
                    }
                }
            }
            return os.str();
        }

        std::vector<SearchBenchPosition> build_default_suite()
        {
            return {
                // ── Core suite (original 10) ─────────────────────────────────────────
                {"startpos", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"},
                {"kiwipete", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"},
                {"audit_p1", "4k2r/ppp2ppp/8/8/3qP3/8/PPP2PPP/4K2R w Kk - 0 1"},
                {"audit_p2", "r3k3/ppp2ppp/8/2ppp3/3R4/8/PPP2PPP/4K3 w - - 0 1"},
                {"audit_p4", "8/p4k2/3n4/3P4/8/8/P4K2/8 w - - 0 1"},
                {"audit_p6", "r1bq1rk1/pp2ppbp/2pp1np1/8/2PPP3/2N2N2/PP2BPPP/R1BQ1RK1 b - - 0 9"},
                {"audit_p7", "3r2k1/pp3ppp/8/8/8/8/PPPrrPPP/3R2K1 w - - 0 1"},
                {"audit_p8", "2r3k1/5pp1/p2p1n1p/1p1P4/1P2P3/P1N2N1P/5PP1/2R3K1 w - - 0 1"},
                {"audit_p9", "r1bq1rk1/pp3ppp/2n1pn2/2pp4/3P4/2PBPN2/PP3PPP/R1BQ1RK1 w - - 0 8"},
                {"audit_p10", "6k1/5pp1/8/3r4/3R4/8/5PP1/6K1 w - - 0 1"},
                // ── Extended suite (15 additional — diverse phases and structures) ────
                // Open game middlegame with bishop pair tension
                {"pos_complex_mg", "r2q1rk1/pp2ppbp/2np1np1/8/3PP3/2N2N2/PP2BPPP/R1BQ1RK1 w - - 0 9"},
                // Sicilian-type: central tension, piece activity
                {"pos_sicilian", "r1bqkb1r/pp3ppp/2npbn2/4p3/3PP3/2N2N2/PPP2PPP/R1BQKB1R w KQkq - 0 7"},
                // Rook endgame: active rook, passed pawn, knight vs rook
                {"pos_rook_eg", "5rk1/R4pp1/5n1p/8/8/5N1P/5PP1/5RK1 w - - 0 1"},
                // Symmetric pawn endgame: king activity decides
                {"pos_pawn_eg", "8/3k1ppp/8/3K1PPP/8/8/8/8 w - - 0 1"},
                // Queen endgame: mating patterns, fortress detection
                {"pos_queens_eg", "8/5k2/8/4q3/4Q3/8/5K2/8 w - - 0 1"},
                // Opposite-color bishops + active kings
                {"pos_opp_bishops", "8/3k2pp/5p2/8/3b4/3B4/4K2P/8 w - - 0 1"},
                // Rook vs advanced passed pawn (classic endgame test)
                {"pos_rook_vs_pawn", "8/8/8/R7/k7/8/1p6/1K6 b - - 0 1"},
                // King and pawn race (tempo sensitivity)
                {"pos_king_race", "8/6pk/8/8/8/8/KP6/8 w - - 0 1"},
                // Complex middlegame: rooks doubled, queen activity, open files
                {"pos_complex_pcs", "r3r1k1/pp3pbp/2pp1np1/q3p3/2P1P3/2NP2PP/PP1Q1PB1/R3R1K1 w - - 0 14"},
                // Hanging pawns structure: c4/d4 under pressure
                {"pos_hanging_pawns", "r2q1rk1/pp1bppbp/2np1np1/8/2pPP3/2N2N2/PP2BPPP/R1BQ1RK1 w - - 0 10"},
                // Ruy Lopez: bishop a4 pin, uncastled black king
                {"pos_ruy_lopez", "r1bqk2r/1bpp1ppp/p1n2n2/1p2p3/B3P3/5N2/PPPP1PPP/RNBQR1K1 b kq - 0 7"},
                // Tactical: pin on f6, potential piece sacrifice (WAC-001)
                {"pos_tactics_wac", "2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - 0 1"},
                // Bishop endgame: two bishops vs two pawns, coordination
                {"pos_eg_bishops", "8/3k4/8/3p1p2/3B1B2/8/4K3/8 w - - 0 1"},
                // Rook endgame: passed a-pawn, rook activity
                {"pos_rook_ending", "1r4k1/5ppp/8/pP6/8/8/5PPP/1R4K1 w - - 0 1"},
                // Symmetric pawn flanks: king march endgame
                {"pos_sym_pawns", "6k1/pp4pp/8/8/8/8/PP4PP/6K1 w - - 0 1"},
            };
        }

        void accumulate_profile(SearchProfile &dst, const SearchProfile &src)
        {
            dst.cycle_counters_enabled = dst.cycle_counters_enabled || src.cycle_counters_enabled;
            dst.threads = std::max(dst.threads, src.threads);
            dst.nodes += src.nodes;
            dst.cycles_total += src.cycles_total;
            dst.cycles_nnue += src.cycles_nnue;
            dst.cycles_movegen += src.cycles_movegen;
            dst.cycles_do_move += src.cycles_do_move;
            dst.cycles_undo_move += src.cycles_undo_move;
            dst.cycles_see += src.cycles_see;
            dst.cycles_gcheck += src.cycles_gcheck;
            dst.diag += src.diag;
            dst.nnue.cycle_counters_enabled = dst.nnue.cycle_counters_enabled || src.nnue.cycle_counters_enabled;
            dst.nnue.loaded = src.nnue.loaded;
            dst.nnue.runtime_enabled = src.nnue.runtime_enabled;
            dst.nnue.eval_calls += src.nnue.eval_calls;
            dst.nnue.fallback_calls += src.nnue.fallback_calls;
            dst.nnue.refresh_calls += src.nnue.refresh_calls;
            dst.nnue.forward_calls += src.nnue.forward_calls;
            dst.nnue.incremental_updates += src.nnue.incremental_updates;
            dst.nnue.capture_updates += src.nnue.capture_updates;
            dst.nnue.full_updates += src.nnue.full_updates;
            dst.nnue.null_updates += src.nnue.null_updates;
            dst.nnue.cycles_refresh += src.nnue.cycles_refresh;
            dst.nnue.cycles_forward += src.nnue.cycles_forward;
            dst.nnue.cycles_update += src.nnue.cycles_update;
            dst.nnue.cycles_full_update += src.nnue.cycles_full_update;
            dst.nnue.cycles_null_update += src.nnue.cycles_null_update;
        }

        void print_profile_json(std::ostream &os, const SearchProfile &p, int indent)
        {
            const std::string pad(indent, ' ');
            const std::string pad2(indent + 2, ' ');
            const std::string pad4(indent + 4, ' ');
            os << pad << "\"profile\": {\n";
            os << pad2 << "\"cycle_counters_enabled\": " << (p.cycle_counters_enabled ? "true" : "false") << ",\n";
            os << pad2 << "\"threads\": " << p.threads << ",\n";
            os << pad2 << "\"nodes\": " << p.nodes << ",\n";
            os << pad2 << "\"cycles_total\": " << p.cycles_total << ",\n";
            os << pad2 << "\"cycles\": {\n";
            os << pad4 << "\"nnue\": " << p.cycles_nnue << ",\n";
            os << pad4 << "\"movegen\": " << p.cycles_movegen << ",\n";
            os << pad4 << "\"do_move\": " << p.cycles_do_move << ",\n";
            os << pad4 << "\"undo_move\": " << p.cycles_undo_move << ",\n";
            os << pad4 << "\"see\": " << p.cycles_see << ",\n";
            os << pad4 << "\"gcheck\": " << p.cycles_gcheck << "\n";
            os << pad2 << "},\n";
            os << pad2 << "\"diag\": {\n";
            os << pad4 << "\"alpha_beta_nodes\": " << p.diag.alpha_beta_nodes << ",\n";
            os << pad4 << "\"quiescence_nodes\": " << p.diag.quiescence_nodes << ",\n";
            os << pad4 << "\"tt_probes\": " << p.diag.tt_probes << ",\n";
            os << pad4 << "\"tt_hits\": " << p.diag.tt_hits << ",\n";
            os << pad4 << "\"tt_cutoffs\": " << p.diag.tt_cutoffs << ",\n";
            os << pad4 << "\"null_move_cuts\": " << p.diag.null_move_cuts << ",\n";
            os << pad4 << "\"rfp_cuts\": " << p.diag.rfp_cuts << ",\n";
            os << pad4 << "\"futility_prunes\": " << p.diag.futility_prunes << ",\n";
            os << pad4 << "\"lmp_prunes\": " << p.diag.lmp_prunes << ",\n";
            os << pad4 << "\"see_prunes\": " << p.diag.see_prunes << ",\n";
            os << pad4 << "\"hist_prunes\": " << p.diag.hist_prunes << ",\n";
            os << pad4 << "\"lmr_searches\": " << p.diag.lmr_searches << ",\n";
            os << pad4 << "\"lmr_re_searches\": " << p.diag.lmr_re_searches << ",\n";
            os << pad4 << "\"razoring_cuts\": " << p.diag.razoring_cuts << ",\n";
            os << pad4 << "\"probcut_cuts\": " << p.diag.probcut_cuts << "\n";
            os << pad2 << "},\n";
            os << pad2 << "\"nnue\": {\n";
            os << pad4 << "\"cycle_counters_enabled\": " << (p.nnue.cycle_counters_enabled ? "true" : "false") << ",\n";
            os << pad4 << "\"loaded\": " << (p.nnue.loaded ? "true" : "false") << ",\n";
            os << pad4 << "\"runtime_enabled\": " << (p.nnue.runtime_enabled ? "true" : "false") << ",\n";
            os << pad4 << "\"eval_calls\": " << p.nnue.eval_calls << ",\n";
            os << pad4 << "\"fallback_calls\": " << p.nnue.fallback_calls << ",\n";
            os << pad4 << "\"refresh_calls\": " << p.nnue.refresh_calls << ",\n";
            os << pad4 << "\"forward_calls\": " << p.nnue.forward_calls << ",\n";
            os << pad4 << "\"incremental_updates\": " << p.nnue.incremental_updates << ",\n";
            os << pad4 << "\"capture_updates\": " << p.nnue.capture_updates << ",\n";
            os << pad4 << "\"full_updates\": " << p.nnue.full_updates << ",\n";
            os << pad4 << "\"null_updates\": " << p.nnue.null_updates << ",\n";
            os << pad4 << "\"cycles_refresh\": " << p.nnue.cycles_refresh << ",\n";
            os << pad4 << "\"cycles_forward\": " << p.nnue.cycles_forward << ",\n";
            os << pad4 << "\"cycles_update\": " << p.nnue.cycles_update << ",\n";
            os << pad4 << "\"cycles_full_update\": " << p.nnue.cycles_full_update << ",\n";
            os << pad4 << "\"cycles_null_update\": " << p.nnue.cycles_null_update << "\n";
            os << pad2 << "}\n";
            os << pad << "}";
        }

    } // namespace

    std::vector<SearchBenchPosition> default_search_suite()
    {
        return build_default_suite();
    }

    std::vector<SearchBenchPosition> load_search_suite_file(const std::string &path)
    {
        std::ifstream f(path);
        if (!f)
        {
            throw std::runtime_error("Unable to open suite file: " + path);
        }

        std::vector<SearchBenchPosition> suite;
        std::string line;
        int line_no = 0;
        while (std::getline(f, line))
        {
            ++line_no;
            line = trim(line);
            if (line.empty() || line[0] == '#')
                continue;

            SearchBenchPosition pos;
            const auto pipe = line.find('|');
            if (pipe == std::string::npos)
            {
                pos.name = "pos" + std::to_string(static_cast<int>(suite.size()) + 1);
                pos.fen = line;
            }
            else
            {
                pos.name = trim(line.substr(0, pipe));
                pos.fen = trim(line.substr(pipe + 1));
            }

            if (pos.name.empty() || pos.fen.empty())
            {
                throw std::runtime_error("Malformed suite line " + std::to_string(line_no) + " in " + path);
            }
            suite.push_back(std::move(pos));
        }

        if (suite.empty())
        {
            throw std::runtime_error("Suite file contained no benchmark positions: " + path);
        }
        return suite;
    }

    SearchBenchSummary run_search_benchmark(const std::vector<SearchBenchPosition> &suite,
                                            const SearchBenchConfig &config)
    {
        SearchBenchSummary summary;
        summary.config = config;

        if (suite.empty())
        {
            return summary;
        }

        Search.set_threads(config.threads);
        Search.set_mode(config.mode);
        const bool prev_profile_enabled = Search.profile_enabled();
        const bool prev_nnue_profile_enabled = NNUE::nnue_profile_enabled();
        Search.set_profile_enabled(config.collect_profile);
        NNUE::nnue_set_profile_enabled(config.collect_profile);
        TT.resize(config.hash_mb);

        const bool prev_info_enabled = Search.info_enabled();
        Search.set_info_enabled(false);

        try
        {
            if (config.warmup_depth > 0)
            {
                if (bench_trace_enabled())
                {
                    std::cerr << "bench-trace warmup-start depth=" << config.warmup_depth << "\n";
                }
                Board warmup_board;
                warmup_board.set_fen(suite.front().fen);
                SearchLimits warmup_limits;
                warmup_limits.depth = config.warmup_depth;
                TT.clear();
                Search.clear_all();
                (void)Search.search(warmup_board, warmup_limits);
                if (bench_trace_enabled())
                {
                    std::cerr << "bench-trace warmup-done\n";
                }
            }

            for (int rep = 0; rep < std::max(1, config.repeat); ++rep)
            {
                for (const auto &pos : suite)
                {
                    if (config.clear_tt_each_position)
                        TT.clear();
                    if (config.clear_history_each_position)
                        Search.clear_all();

                    Board board;
                    board.set_fen(pos.fen);

                    SearchLimits limits;
                    limits.depth = config.depth;

                    if (bench_trace_enabled())
                    {
                        std::cerr << "bench-trace case-start name=" << pos.name
                                  << " rep=" << (rep + 1)
                                  << " depth=" << config.depth
                                  << " threads=" << config.threads
                                  << " mode=" << (config.mode == ROOT_SPLIT ? "rootsplit" : "lazysmp")
                                  << " hash=" << config.hash_mb
                                  << "\n";
                    }

                    auto t0 = std::chrono::steady_clock::now();
                    Move best = Search.search(board, limits);
                    auto elapsed_us = std::chrono::duration_cast<std::chrono::microseconds>(
                                          std::chrono::steady_clock::now() - t0)
                                          .count();
                    auto elapsed_ms = elapsed_us / 1000;

                    SearchStats stats = Search.get_stats();
                    SearchProfile profile = Search.last_profile();
                    SearchSmpStats smp = Search.last_smp();

                    SearchBenchCaseResult r;
                    r.name = (config.repeat > 1)
                                 ? pos.name + "#" + std::to_string(rep + 1)
                                 : pos.name;
                    r.fen = pos.fen;
                    r.bestmove = best ? best.to_uci() : "0000";
                    r.ponder = stats.ponder_move ? stats.ponder_move.to_uci() : "";
                    r.depth = stats.depth;
                    r.seldepth = stats.seldepth;
                    r.score_cp = stats.score;
                    r.nodes = stats.nodes;
                    r.elapsed_us = elapsed_us;
                    r.elapsed_ms = elapsed_ms;
                    r.nps = (elapsed_us > 0) ? (stats.nodes * 1000000 / elapsed_us) : 0;
                    r.smp = smp;
                    if (config.collect_profile)
                    {
                        r.has_profile = true;
                        r.profile = profile;
                        summary.has_profile = true;
                        accumulate_profile(summary.aggregate_profile, profile);
                    }

                    summary.total_nodes += r.nodes;
                    summary.total_elapsed_us += r.elapsed_us;
                    summary.total_elapsed_ms += r.elapsed_ms;
                    if (bench_trace_enabled())
                    {
                        std::cerr << "bench-trace case-done name=" << r.name
                                  << " bestmove=" << r.bestmove
                                  << " nodes=" << r.nodes
                                  << " elapsed_us=" << r.elapsed_us
                                  << " nps=" << r.nps
                                  << "\n";
                    }
                    summary.cases.push_back(std::move(r));
                }
            }
        }
        catch (...)
        {
            Search.set_profile_enabled(prev_profile_enabled);
            NNUE::nnue_set_profile_enabled(prev_nnue_profile_enabled);
            Search.set_info_enabled(prev_info_enabled);
            throw;
        }

        Search.set_profile_enabled(prev_profile_enabled);
        NNUE::nnue_set_profile_enabled(prev_nnue_profile_enabled);
        Search.set_info_enabled(prev_info_enabled);

        if (!summary.cases.empty())
        {
            std::vector<int64_t> nps_values;
            nps_values.reserve(summary.cases.size());
            for (const auto &c : summary.cases)
            {
                nps_values.push_back(c.nps);
            }
            std::sort(nps_values.begin(), nps_values.end());
            summary.min_nps = nps_values.front();
            summary.max_nps = nps_values.back();
            summary.median_nps = nps_values[nps_values.size() / 2];
        }
        summary.total_nps = (summary.total_elapsed_us > 0)
                                ? (summary.total_nodes * 1000000 / summary.total_elapsed_us)
                                : 0;

        return summary;
    }

    void print_search_benchmark_text(std::ostream &os, const SearchBenchSummary &summary)
    {
        const char *mode_name = (summary.config.mode == ROOT_SPLIT) ? "RootSplit" : "LazySMP";
        os << "Redux Search Benchmark\n";
        os << "======================\n";
        os << "Positions: " << summary.cases.size() << "\n";
        os << "Depth:     " << summary.config.depth << "\n";
        os << "Threads:   " << summary.config.threads << "\n";
        os << "Mode:      " << mode_name << "\n";
        os << "Hash:      " << summary.config.hash_mb << " MB\n";
        os << "Repeat:    " << summary.config.repeat << "\n";
        os << "Warmup:    " << summary.config.warmup_depth << "\n\n";

        os << std::left
           << std::setw(14) << "Name"
           << std::right
           << std::setw(8) << "Depth"
           << std::setw(10) << "Sel"
           << std::setw(14) << "Nodes"
           << std::setw(10) << "Time"
           << std::setw(14) << "NPS"
           << std::setw(10) << "Score"
           << std::setw(12) << "BestMove"
           << "\n";
        os << std::string(92, '-') << "\n";

        for (const auto &c : summary.cases)
        {
            os << std::left << std::setw(14) << c.name
               << std::right
               << std::setw(8) << c.depth
               << std::setw(10) << c.seldepth
               << std::setw(14) << c.nodes
               << std::setw(10) << c.elapsed_ms
               << std::setw(14) << c.nps
               << std::setw(10) << c.score_cp
               << std::setw(12) << c.bestmove
               << "\n";
            if (c.smp.threads > 1)
            {
                os << "  smp: threads=" << c.smp.threads << "\n";
            }
        }

        os << std::string(92, '-') << "\n";
        os << "Total nodes:  " << summary.total_nodes << "\n";
        os << "Total time:   " << summary.total_elapsed_ms << " ms"
           << " (" << summary.total_elapsed_us << " us)\n";
        os << "Total NPS:    " << summary.total_nps << "\n";
        os << "Median NPS:   " << summary.median_nps << "\n";
        os << "Min/Max NPS:  " << summary.min_nps << " / " << summary.max_nps << "\n";
        if (summary.has_profile)
        {
            const auto &p = summary.aggregate_profile;
            os << "\nProfile summary:\n";
            os << "  AB/Q nodes:   " << p.diag.alpha_beta_nodes << " / " << p.diag.quiescence_nodes << "\n";
            os << "  TT probes:    " << p.diag.tt_hits << " hits / " << p.diag.tt_probes << " probes\n";
            os << "  Cycles total: " << p.cycles_total << "\n";
            os << "  Hotspots:     nnue=" << p.cycles_nnue
               << " movegen=" << p.cycles_movegen
               << " do_move=" << p.cycles_do_move
               << " undo_move=" << p.cycles_undo_move
               << " see=" << p.cycles_see
               << " gcheck=" << p.cycles_gcheck << "\n";
            os << "  NNUE:         evals=" << p.nnue.eval_calls
               << " refresh=" << p.nnue.refresh_calls
               << " forward=" << p.nnue.forward_calls
               << " updates=" << p.nnue.incremental_updates
               << " full_updates=" << p.nnue.full_updates << "\n";
        }
    }

    void print_search_benchmark_json(std::ostream &os, const SearchBenchSummary &summary)
    {
        const char *mode_name = (summary.config.mode == ROOT_SPLIT) ? "RootSplit" : "LazySMP";
        os << "{\n";
        os << "  \"config\": {\n";
        os << "    \"depth\": " << summary.config.depth << ",\n";
        os << "    \"threads\": " << summary.config.threads << ",\n";
        os << "    \"hash_mb\": " << summary.config.hash_mb << ",\n";
        os << "    \"repeat\": " << summary.config.repeat << ",\n";
        os << "    \"warmup_depth\": " << summary.config.warmup_depth << ",\n";
        os << "    \"mode\": \"" << mode_name << "\",\n";
        os << "    \"collect_profile\": " << (summary.config.collect_profile ? "true" : "false") << "\n";
        os << "  },\n";
        os << "  \"summary\": {\n";
        os << "    \"positions\": " << summary.cases.size() << ",\n";
        os << "    \"total_nodes\": " << summary.total_nodes << ",\n";
        os << "    \"total_elapsed_us\": " << summary.total_elapsed_us << ",\n";
        os << "    \"total_elapsed_ms\": " << summary.total_elapsed_ms << ",\n";
        os << "    \"total_nps\": " << summary.total_nps << ",\n";
        os << "    \"median_nps\": " << summary.median_nps << ",\n";
        os << "    \"min_nps\": " << summary.min_nps << ",\n";
        os << "    \"max_nps\": " << summary.max_nps;
        if (summary.has_profile)
            os << ",\n";
        else
            os << "\n";
        if (summary.has_profile)
        {
            print_profile_json(os, summary.aggregate_profile, 4);
            os << "\n";
        }
        os << "  },\n";
        os << "  \"cases\": [\n";
        for (size_t i = 0; i < summary.cases.size(); ++i)
        {
            const auto &c = summary.cases[i];
            os << "    {\n";
            os << "      \"name\": \"" << json_escape(c.name) << "\",\n";
            os << "      \"fen\": \"" << json_escape(c.fen) << "\",\n";
            os << "      \"depth\": " << c.depth << ",\n";
            os << "      \"seldepth\": " << c.seldepth << ",\n";
            os << "      \"score_cp\": " << c.score_cp << ",\n";
            os << "      \"nodes\": " << c.nodes << ",\n";
            os << "      \"elapsed_us\": " << c.elapsed_us << ",\n";
            os << "      \"elapsed_ms\": " << c.elapsed_ms << ",\n";
            os << "      \"nps\": " << c.nps << ",\n";
            os << "      \"bestmove\": \"" << json_escape(c.bestmove) << "\",\n";
            os << "      \"ponder\": \"" << json_escape(c.ponder) << "\",\n";
            os << "      \"smp\": {\n";
            os << "        \"threads\": " << c.smp.threads << "\n";
            os << "      }";
            if (c.has_profile)
                os << ",\n";
            else
                os << "\n";
            if (c.has_profile)
            {
                print_profile_json(os, c.profile, 6);
                os << "\n";
            }
            os << "    }" << (i + 1 < summary.cases.size() ? "," : "") << "\n";
        }
        os << "  ]\n";
        os << "}\n";
    }

} // namespace Chess::Benchmark