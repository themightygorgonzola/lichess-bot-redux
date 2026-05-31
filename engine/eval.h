#pragma once

#include "board.h"

// ============================================================================
// Static evaluation of a chess position
// Returns score in centipawns from the perspective of the side to move.
// Positive = good for side to move, negative = bad.
// ============================================================================

namespace Chess {

// Centipawn piece values indexed by PieceType (NO_PIECE_TYPE=0 .. KING=6).
// Shared between eval and search (e.g. delta pruning in qsearch).
inline constexpr int PIECE_VALUES[PIECE_TYPE_NB] = {
    0, 100, 320, 330, 500, 900, 20000
};

// ── Eval trace (structured per-term breakdown) ──────────────────────────────

// Machine-readable key for each eval term (stable across versions).
// These map 1:1 to the categories in evaluate_explain().
enum EvalTermId {
    EVAL_MATERIAL_PST,
    EVAL_BISHOP_PAIR,
    EVAL_ROOK_FILES,
    EVAL_PAWN_STRUCTURE,
    EVAL_MOBILITY,
    EVAL_ROOK_7TH,
    EVAL_OUTPOSTS,
    EVAL_PINS,
    EVAL_PIN_CREATION,
    EVAL_BAD_BISHOP,
    EVAL_THREATS,
    EVAL_SPACE,
    EVAL_ROOK_BEHIND_PASSER,
    EVAL_KING_PASSER_DIST,
    EVAL_WEAK_MINOR,
    EVAL_KING_SAFETY,
    EVAL_CASTLING,
    EVAL_MOPUP,
    EVAL_TEMPO,
    EVAL_TERM_COUNT  // must be last
};

// Stable JSON key names for each term (index by EvalTermId).
inline constexpr const char* EVAL_TERM_KEYS[EVAL_TERM_COUNT] = {
    "material_pst",
    "bishop_pair",
    "rook_files",
    "pawn_structure",
    "mobility",
    "rook_7th",
    "outposts",
    "pins",
    "pin_creation",
    "bad_bishop",
    "threats",
    "space",
    "rook_behind_passer",
    "king_passer_dist",
    "weak_minor",
    "king_safety",
    "castling",
    "mopup",
    "tempo",
};

struct EvalTrace {
    struct Term { int mg; int eg; };

    Term   terms[EVAL_TERM_COUNT]{};   // per-term MG/EG (White POV)
    int    phase       = 0;            // game phase 0..256
    int    mg_total    = 0;            // sum of all MG
    int    eg_total    = 0;            // sum of all EG
    int    blended     = 0;            // (mg*phase + eg*(256-phase))/256, White POV
    int    scale_factor = 256;         // endgame draw scaling 0..256 (256 = no change)
    int    stm_score   = 0;            // final score, side-to-move POV

    // Compute blended value for a single term
    int blend(int idx) const {
        return (terms[idx].mg * phase + terms[idx].eg * (256 - phase)) / 256;
    }
};

// ── Public API ──────────────────────────────────────────────────────────────

// Full evaluation
int evaluate(const Board& board);

// Full evaluation with term-by-term breakdown printed to stdout.
// Returns the same value as evaluate().
int evaluate_explain(const Board& board);

// Structured trace: same eval logic as evaluate_explain(), returns all
// per-term MG/EG values plus phase and totals.  No I/O.
EvalTrace evaluate_trace(const Board& board);

// Material-only evaluation (fast, for testing)
int evaluate_material(const Board& board);

// Initialize evaluation tables
void eval_init();

} // namespace Chess
