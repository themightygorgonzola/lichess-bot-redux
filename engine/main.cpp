#include "bitboard.h"
#include "benchmark.h"
#include "board.h"
#include "eval.h"
#include "movegen.h"
#include "search.h"
#include "tt.h"
#include "uci.h"
#include "nnue/nnue_eval.h"
#include <iostream>
#include <cstdlib>
#include <chrono>
#include <fstream>
#include <vector>
#include <string>
#ifdef _WIN32
#include <windows.h>
#endif

namespace {
#ifdef _WIN32
bool maybe_apply_affinity_mask(int argc, char* argv[]) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::string(argv[i]) == "--affinity-mask") {
            std::string s = argv[i + 1];
            char* end = nullptr;
            unsigned long long mask = std::strtoull(s.c_str(), &end, 0);
            if (end == s.c_str() || *end != '\0' || mask == 0ULL) {
                std::cerr << "Invalid --affinity-mask value: " << s << "\n";
                return false;
            }
            if (!SetProcessAffinityMask(GetCurrentProcess(), static_cast<DWORD_PTR>(mask))) {
                std::cerr << "Failed to apply --affinity-mask: " << s << "\n";
                return false;
            }
            break;
        }
    }
    return true;
}
#else
bool maybe_apply_affinity_mask(int, char**) { return true; }
#endif

bool is_top_level_mode_arg(const std::string& arg) {
    return arg == "--test"
        || arg == "--bench"
        || arg == "--bench-search"
        || arg == "--divide"
        || arg == "--debug";
}
}

#ifndef DISABLE_NNUE
static void maybe_load_cli_eval(int argc, char* argv[]) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (std::string(argv[i]) == "--eval") {
            Chess::NNUE::nnue_init(argv[i + 1]);
            return;
        }
    }
    Chess::NNUE::nnue_auto_discover(argv[0]);
}
#endif

// ============================================================================
// Perft test suite — well-known positions with verified node counts
// ============================================================================
struct PerftTest {
    const char* fen;
    int         depth;
    uint64_t    expected;
    const char* label;
};

static const std::vector<PerftTest> PERFT_SUITE = {
    // Startpos
    {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 1, 20,       "startpos d1"},
    {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 2, 400,      "startpos d2"},
    {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 3, 8902,     "startpos d3"},
    {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 4, 197281,   "startpos d4"},
    {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 5, 4865609,  "startpos d5"},

    // Kiwipete — exercises en passant, castling, promotions, pins, double check
    {"r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 1, 48,      "kiwipete d1"},
    {"r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 2, 2039,    "kiwipete d2"},
    {"r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 3, 97862,   "kiwipete d3"},
    {"r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 4, 4085603, "kiwipete d4"},

    // Position 3 — tests en passant, discovered check
    {"8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 1, 14,    "pos3 d1"},
    {"8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 2, 191,   "pos3 d2"},
    {"8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 3, 2812,  "pos3 d3"},
    {"8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 4, 43238, "pos3 d4"},
    {"8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 5, 674624,"pos3 d5"},

    // Position 4 — lots of promotions, underpromotions
    {"r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 1, 6,      "pos4 d1"},
    {"r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 2, 264,    "pos4 d2"},
    {"r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 3, 9467,   "pos4 d3"},
    {"r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 4, 422333, "pos4 d4"},

    // Position 5 — castling rights, promotions
    {"rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", 1, 44,      "pos5 d1"},
    {"rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", 2, 1486,    "pos5 d2"},
    {"rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", 3, 62379,   "pos5 d3"},
    {"rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", 4, 2103487, "pos5 d4"},
};

// ============================================================================
// Perft divide — shows node count per root move (debugging tool)
// ============================================================================
static void perft_divide(Chess::Board& board, int depth) {
    using namespace Chess;
    MoveList legal;
    generate_legal(board, legal);

    uint64_t total = 0;
    for (int i = 0; i < legal.size(); ++i) {
        StateInfo state;
        board.do_move(legal[i].move, state);
        uint64_t count = (depth <= 1) ? 1 : perft(board, depth - 1);
        board.undo_move(legal[i].move);
        total += count;
        std::cout << legal[i].move.to_uci() << ": " << count << "\n";
    }
    std::cout << "\nMoves: " << legal.size() << "\nTotal: " << total << std::endl;
}

// ============================================================================
// Board consistency checker — call after do_move/undo_move to detect corruption
// ============================================================================
static bool verify_board(const Chess::Board& board, const char* context) {
    using namespace Chess;
    if (!board.is_valid()) {
        std::cerr << "BOARD CORRUPT after " << context << "\n";
        board.print();
        return false;
    }
    // Verify piece arrays match the mailbox
    for (Square s = A1; s < Square(SQUARE_NB); ++s) {
        Piece p = board.piece_on(s);
        if (p != NO_PIECE) {
            if (!(board.pieces(color_of(p), type_of(p)) & square_bb(s))) {
                std::cerr << "BITBOARD MISMATCH at " << square_to_string(s)
                          << " after " << context << "\n";
                board.print();
                return false;
            }
        } else {
            if (board.pieces() & square_bb(s)) {
                std::cerr << "GHOST PIECE at " << square_to_string(s)
                          << " after " << context << "\n";
                board.print();
                return false;
            }
        }
    }
    return true;
}

// ============================================================================
// Validated perft — runs consistency checks at every node (slow, for debugging)
// ============================================================================
static uint64_t perft_debug(Chess::Board& board, int depth) {
    using namespace Chess;
    if (depth == 0) return 1;

    MoveList legal;
    generate_legal(board, legal);
    if (depth == 1) return legal.count;

    uint64_t nodes = 0;
    for (int i = 0; i < legal.count; ++i) {
        StateInfo state;
        std::string fen_before = board.to_fen();
        board.do_move(legal[i].move, state);

        if (!verify_board(board, legal[i].move.to_uci().c_str())) {
            std::cerr << "  FEN before: " << fen_before << "\n";
            return 0;
        }

        nodes += perft_debug(board, depth - 1);
        board.undo_move(legal[i].move);

        // Verify undo restored the board correctly
        std::string fen_after = board.to_fen();
        if (fen_before != fen_after) {
            std::cerr << "UNDO MISMATCH for " << legal[i].move.to_uci() << "\n"
                      << "  Before: " << fen_before << "\n"
                      << "  After:  " << fen_after << "\n";
            return 0;
        }
    }
    return nodes;
}

int main(int argc, char* argv[]) {
    if (!maybe_apply_affinity_mask(argc, argv))
        return 1;

    // Initialize all subsystems
    Chess::bitboards_init();
    Chess::Board::init_zobrist();
    Chess::eval_init();

    // Default: 64 MB hash table
    Chess::TT.resize(64);

    // NNUE weights are loaded via 'setoption name EvalFile' in UCI mode,
    // or can be triggered by passing --eval <path> from the CLI below.
    // No auto-load here to avoid confusing errors when cwd != engine dir.

    std::string mode;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--affinity-mask" || arg == "--eval") {
            ++i; // skip option value
            continue;
        }
        if (arg == "--test" || arg == "--bench" || arg == "--bench-search") {
            mode = arg;
            break;
        }
    }

    // --test : run the full perft test suite
    if (mode == "--test") {
        std::cout << "Redux  Perft Test Suite\n";
        std::cout << "=======================\n\n";

        int passed = 0, failed = 0;
        auto suite_start = std::chrono::steady_clock::now();

        for (auto& t : PERFT_SUITE) {
            Chess::Board board;
            board.set_fen(t.fen);

            auto t0 = std::chrono::steady_clock::now();
            uint64_t nodes = Chess::perft(board, t.depth);
            auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::steady_clock::now() - t0).count();

            bool ok = (nodes == t.expected);
            std::cout << (ok ? "  PASS" : "**FAIL")
                      << "  " << t.label
                      << "  got=" << nodes
                      << "  exp=" << t.expected
                      << "  " << ms << "ms\n";
            if (ok) ++passed; else ++failed;
        }

        auto total_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::steady_clock::now() - suite_start).count();
        std::cout << "\n" << passed << " passed, " << failed << " failed  ("
                  << total_ms << " ms total)\n";
        return failed ? 1 : 0;
    }

    // --bench [--threads N] [--depth D] [--mode lazysmp|rootsplit] : quick benchmark
    if (mode == "--bench") {
        int bench_threads = 1;
        int bench_depth = 8;
        Chess::SearchMode bench_mode = Chess::LAZY_SMP;
        
        // Parse optional parameters
        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i];
            if (is_top_level_mode_arg(arg)) {
                continue;
            }
            if (arg == "--affinity-mask" || arg == "--eval") {
                ++i;
                continue;
            }
            if (arg == "--threads" && i + 1 < argc) {
                bench_threads = std::atoi(argv[++i]);
            } else if (arg == "--depth" && i + 1 < argc) {
                bench_depth = std::atoi(argv[++i]);
            } else if (arg == "--mode" && i + 1 < argc) {
                std::string m = argv[++i];
                if (m == "rootsplit" || m == "RootSplit" || m == "root")
                    bench_mode = Chess::ROOT_SPLIT;
                else
                    bench_mode = Chess::LAZY_SMP;
            }
        }
        
        bench_threads = std::max(1, std::min(bench_threads, 256));
        bench_depth = std::max(1, std::min(bench_depth, Chess::MAX_PLY - 1));
        
        // Set up engine with proper TT size (32MB per thread, capped at 1GB)
        int tt_mb = std::min(32 * bench_threads, 1024);
        Chess::TT.resize(tt_mb);
        Chess::Search.set_threads(bench_threads);
        Chess::Search.set_mode(bench_mode);
        
        const char* mode_name = (bench_mode == Chess::ROOT_SPLIT) ? "RootSplit" : "LazySMP";
        std::cout << "Redux Benchmark\n";
        std::cout << "===============\n";
        std::cout << "Threads: " << bench_threads << "\n";
        std::cout << "Mode:    " << mode_name << "\n";
        std::cout << "TT Size: " << tt_mb << " MB\n";

        Chess::Board board;
        board.set_startpos();

        std::cout << "\nPerft(5) from startpos...\n";
        auto start = std::chrono::steady_clock::now();
        uint64_t nodes = Chess::perft(board, 5);
        auto elapsed = std::chrono::steady_clock::now() - start;
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count();

        std::cout << "Nodes: " << nodes << "\n";
        std::cout << "Time:  " << ms << " ms\n";
        if (ms > 0) std::cout << "NPS:   " << (nodes * 1000 / ms) << "\n";
        std::cout << "Expected: 4865609\n";
        std::cout << (nodes == 4865609 ? "PASS" : "FAIL") << "\n";

        std::cout << "\nSearch test (depth " << bench_depth << ")...\n";
        board.set_startpos();
        Chess::SearchLimits limits;
        limits.depth = bench_depth;
        Chess::Move best = Chess::Search.search(board, limits);
        std::cout << "Best move: " << best.to_uci() << "\n";
        return 0;
    }

    // --bench-search [--threads N] [--depth D] [--hash MB] [--repeat R]
    //                [--mode lazysmp|rootsplit] [--suite-file PATH] [--json] [--profile]
    if (mode == "--bench-search") {
#ifndef DISABLE_NNUE
        Chess::NNUE::nnue_set_info_enabled(false);
        maybe_load_cli_eval(argc, argv);
#endif
        Chess::Benchmark::SearchBenchConfig config;
        std::string suite_file;
        std::string json_file;
        bool json = false;

        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i];
            if (is_top_level_mode_arg(arg)) {
                continue;
            }
            if (arg == "--threads" && i + 1 < argc) {
                config.threads = std::atoi(argv[++i]);
            } else if (arg == "--depth" && i + 1 < argc) {
                config.depth = std::atoi(argv[++i]);
            } else if (arg == "--hash" && i + 1 < argc) {
                config.hash_mb = std::atoi(argv[++i]);
            } else if (arg == "--repeat" && i + 1 < argc) {
                config.repeat = std::atoi(argv[++i]);
            } else if (arg == "--warmup-depth" && i + 1 < argc) {
                config.warmup_depth = std::atoi(argv[++i]);
            } else if (arg == "--mode" && i + 1 < argc) {
                std::string m = argv[++i];
                config.mode = (m == "rootsplit" || m == "RootSplit" || m == "root")
                    ? Chess::ROOT_SPLIT
                    : Chess::LAZY_SMP;
            } else if (arg == "--suite-file" && i + 1 < argc) {
                suite_file = argv[++i];
            } else if (arg == "--json") {
                json = true;
            } else if (arg == "--json-file" && i + 1 < argc) {
                json_file = argv[++i];
            } else if (arg == "--profile") {
                config.collect_profile = true;
            } else if (arg == "--keep-tt") {
                config.clear_tt_each_position = false;
            } else if (arg == "--keep-history") {
                config.clear_history_each_position = false;
            } else if (arg == "--eval") {
                ++i; // already handled by maybe_load_cli_eval()
            } else if (arg == "--affinity-mask") {
                ++i; // already handled by maybe_apply_affinity_mask()
            }
        }

        config.threads = std::max(1, std::min(config.threads, 256));
        config.depth = std::max(1, std::min(config.depth, Chess::MAX_PLY - 1));
        config.hash_mb = std::max(1, std::min(config.hash_mb, 4096));
        config.repeat = std::max(1, std::min(config.repeat, 1000));
        config.warmup_depth = std::max(0, std::min(config.warmup_depth, config.depth));

        try {
            auto suite = suite_file.empty()
                ? Chess::Benchmark::default_search_suite()
                : Chess::Benchmark::load_search_suite_file(suite_file);
            auto summary = Chess::Benchmark::run_search_benchmark(suite, config);
            if (json) {
                if (!json_file.empty()) {
                    std::ofstream out(json_file, std::ios::binary);
                    if (!out) {
                        std::cerr << "Benchmark error: cannot open json output file '" << json_file << "'\n";
                        return 1;
                    }
                    Chess::Benchmark::print_search_benchmark_json(out, summary);
                } else {
                    Chess::Benchmark::print_search_benchmark_json(std::cout, summary);
                }
            } else {
                Chess::Benchmark::print_search_benchmark_text(std::cout, summary);
            }
            return 0;
        } catch (const std::exception& ex) {
            std::cerr << "Benchmark error: " << ex.what() << "\n";
            return 1;
        }
    }

    // --divide <depth> [fen] : perft divide from startpos or given FEN
    if (mode == "--divide") {
        int depth = (argc > 2) ? std::atoi(argv[2]) : 5;
        Chess::Board board;
        if (argc > 3) {
            std::string fen;
            for (int i = 3; i < argc; ++i) {
                if (!fen.empty()) fen += ' ';
                fen += argv[i];
            }
            board.set_fen(fen);
        } else {
            board.set_startpos();
        }
        board.print();
        perft_divide(board, depth);
        return 0;
    }

    // --debug <depth> : validated perft with consistency checks at every node
    if (mode == "--debug") {
        int depth = (argc > 2) ? std::atoi(argv[2]) : 3;
        Chess::Board board;
        if (argc > 3) {
            std::string fen;
            for (int i = 3; i < argc; ++i) {
                if (!fen.empty()) fen += ' ';
                fen += argv[i];
            }
            board.set_fen(fen);
        } else {
            board.set_startpos();
        }
        std::cout << "Debug perft(" << depth << ") with board verification...\n";
        board.print();
        auto t0 = std::chrono::steady_clock::now();
        uint64_t nodes = perft_debug(board, depth);
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                      std::chrono::steady_clock::now() - t0).count();
        std::cout << "Nodes: " << nodes << "  (" << ms << " ms)\n";
        std::cout << "Board OK after full traversal.\n";
        return 0;
    }

    // Normal UCI mode
#ifndef DISABLE_NNUE
    maybe_load_cli_eval(argc, argv);
#endif
    Chess::UCI uci;
    uci.loop();

    return 0;
}
