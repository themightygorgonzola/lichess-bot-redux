#pragma once
#include "../board.h"
#include <string>
#include <climits>

// ============================================================================
// Syzygy tablebase interface — C++ wrapper over the Fathom C library.
//
// Call syzygy_init() from UCI setoption(SyzygyPath = ...).
// syzygy_probe_wdl() returns an integer WDL for the given position:
//   +2 = TB_WIN          (engine to move wins)
//   +1 = TB_CURSED_WIN   (win, but drawn by 50-move rule)
//    0 = TB_DRAW
//   -1 = TB_BLESSED_LOSS (loss, but drawn by 50-move rule)
//   -2 = TB_LOSS         (engine to move loses)
//  INT_MIN = probe failed (castling present, rule50 != 0, or TB not loaded)
// ============================================================================

namespace Chess {

bool syzygy_init(const std::string& path);
bool syzygy_enabled();
int  syzygy_piece_limit();   // returns TB_LARGEST (max pieces in loaded tables)
int  syzygy_probe_depth();
void syzygy_set_probe_depth(int d);
void syzygy_set_50move_rule(bool v);

// Core WDL probe — see values above.
int  syzygy_probe_wdl(const Board& b);

} // namespace Chess
