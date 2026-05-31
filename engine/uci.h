#pragma once

#include "board.h"
#include "search.h"
#include <string>
#include <thread>

// ============================================================================
// UCI (Universal Chess Interface) protocol handler
// This allows communication with chess GUIs and bot platforms.
// ============================================================================

namespace Chess
{

    class UCI
    {
    public:
        UCI();

        // Main UCI loop — reads from stdin, writes to stdout
        void loop();

    private:
        void cmd_uci();
        void cmd_isready();
        void cmd_ucinewgame();
        void cmd_position(std::istringstream &is);
        void cmd_go(std::istringstream &is);
        void cmd_stop();
        void cmd_ponderhit(std::istringstream &is);
        void cmd_quit();
        void cmd_setoption(std::istringstream &is);
        void cmd_display();                     // Non-standard: print the board
        void cmd_eval();                        // Non-standard: eval breakdown
        void cmd_evalvec();                     // Non-standard: machine-readable eval vector (JSON)
        void cmd_perft(std::istringstream &is); // Non-standard: perft test
        void cmd_bench(std::istringstream &is); // Non-standard: benchmark suite

        // Wait for running search thread to finish (if any)
        void wait_for_search();

        Board board_;
        bool running_ = true;
        std::thread search_thread_; // managed search thread (not detached)
    };

} // namespace Chess
