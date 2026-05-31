// Syzygy tablebase integration — C++ wrapper over the Fathom C library.

#include "syzygy.h"

// tbprobe.h is a C header — include inside extern "C" to prevent C++ name mangling.
extern "C" {
#include "tbprobe.h"
}

#include <string>
#include <climits>

namespace Chess {

static bool s_enabled      = false;
static int  s_probe_depth  = 1;
static bool s_50move_rule  = true;

// ---------------------------------------------------------------------------
// Initialise tablebases from a path string (semicolon-separated dirs on Windows,
// colon-separated on Linux).  Returns true iff at least one table was loaded.
// ---------------------------------------------------------------------------
bool syzygy_init(const std::string& path) {
    if (path.empty() || path == "<empty>") {
        s_enabled = false;
        return false;
    }
    s_enabled = tb_init(path.c_str()) && (TB_LARGEST > 0);
    return s_enabled;
}

bool syzygy_enabled() {
    return s_enabled && (TB_LARGEST > 0);
}

int syzygy_piece_limit() {
    return static_cast<int>(TB_LARGEST);
}

int syzygy_probe_depth() {
    return s_probe_depth;
}

void syzygy_set_probe_depth(int d) {
    s_probe_depth = d;
}

void syzygy_set_50move_rule(bool v) {
    s_50move_rule = v;
}

// ---------------------------------------------------------------------------
// WDL probe — bridges Board state to Fathom's tb_probe_wdl().
//
// Fathom automatically returns TB_RESULT_FAILED when:
//   • castling_rights != 0 (WDL tablebase doesn't handle castling)
//   • rule50 != 0          (WDL value is not accurate with a live 50-move clock)
// Both conditions produce a graceful failure rather than a wrong score.
// ---------------------------------------------------------------------------
int syzygy_probe_wdl(const Board& b) {
    unsigned ep = (b.ep_square() == NO_SQUARE) ? 0u
                                               : static_cast<unsigned>(b.ep_square());

    unsigned result = tb_probe_wdl(
        static_cast<uint64_t>(b.pieces(WHITE)),
        static_cast<uint64_t>(b.pieces(BLACK)),
        static_cast<uint64_t>(b.pieces(KING)),
        static_cast<uint64_t>(b.pieces(QUEEN)),
        static_cast<uint64_t>(b.pieces(ROOK)),
        static_cast<uint64_t>(b.pieces(BISHOP)),
        static_cast<uint64_t>(b.pieces(KNIGHT)),
        static_cast<uint64_t>(b.pieces(PAWN)),
        static_cast<unsigned>(b.halfmove()),       // rule50
        static_cast<unsigned>(b.castling_rights()),// non-zero → FAILED
        ep,
        b.side_to_move() == WHITE                  // true = white to move
    );

    if (result == TB_RESULT_FAILED) return INT_MIN;

    // Map Fathom's 5-step scale to our signed ints.
    switch (result) {
        case TB_WIN:          return  2;
        case TB_CURSED_WIN:   return  1;
        case TB_DRAW:         return  0;
        case TB_BLESSED_LOSS: return -1;
        case TB_LOSS:         return -2;
        default:              return INT_MIN;
    }
}

} // namespace Chess
