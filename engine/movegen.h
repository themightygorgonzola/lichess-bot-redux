#pragma once

#include "board.h"
#include "move.h"
#include <vector>

// ============================================================================
// Legal move generation
// Generates all pseudo-legal moves, filtered for legality.
// ============================================================================

namespace Chess {

// Move list: stack-allocated, fixed-size array of moves
struct MoveList {
    ScoredMove moves[MAX_MOVES];
    int count = 0;

    void add(Move m) {
        assert(count < MAX_MOVES);
        moves[count++].move = m;
    }

    ScoredMove& operator[](int i) { return moves[i]; }
    const ScoredMove& operator[](int i) const { return moves[i]; }

    int size() const { return count; }

    // Iterator support
    ScoredMove* begin() { return moves; }
    ScoredMove* end()   { return moves + count; }
    const ScoredMove* begin() const { return moves; }
    const ScoredMove* end()   const { return moves + count; }
};

enum GenType {
    ALL_MOVES,       // All legal moves
    CAPTURES_ONLY,   // Only captures + queen promotions (for quiescence)
    QUIET_ONLY       // Only non-captures
};

// Generate pseudo-legal moves (may leave king in check)
void generate_pseudo_legal(const Board& board, MoveList& list, GenType type = ALL_MOVES);

// Generate all legal moves
void generate_legal(const Board& board, MoveList& list, GenType type = ALL_MOVES);

// Count legal moves (for perft testing)
uint64_t perft(Board& board, int depth);

// Check if position has any legal moves
bool has_legal_moves(const Board& board);

} // namespace Chess
