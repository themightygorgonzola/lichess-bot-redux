#pragma once
// ============================================================================
// NNUE Updater v2 — incremental accumulator maintenance (HalfKAv2)
//
// Provides the glue between Board::do_move / undo_move and the NNUE
// accumulator.  All update logic is concentrated here so that board.cpp
// only needs to call a thin wrapper.
//
// The updater works in two modes:
//   1. Full refresh — recompute from the board (first call, or fallback)
//   2. Incremental — copy parent accumulator, then add/sub changed features
//
// HalfKAv2 incremental rules:
//   - King moves: ALWAYS trigger a full refresh (king bucket changes)
//   - Non-king moves: incremental add/sub as before, but feature indices
//     now depend on the king bucket + horizontal mirroring
//
// The accumulator lives inside StateInfo (one per ply on the search stack).
// ============================================================================

#include "nnue_accumulator.h"
#include "nnue_arch.h"
#include "../types.h"
#include "../move.h"

namespace Chess {
class Board;  // forward declaration
}

namespace Chess::NNUE {

class Network;  // forward declaration

// ── HalfKAv2 feature index computation ────────────────────────────────────
//
// Unlike v1 (which used a flat index), HalfKAv2 features depend on the
// king square of the perspective being computed. This structure holds all the
// pre-computed king info for both perspectives.

struct PerspectiveInfo {
    int king_oriented;     // king square after orientation (vertical flip for black + mirror)
    bool mirrored;         // whether horizontal mirroring is active
};

// Compute oriented king info for a given perspective
inline PerspectiveInfo make_perspective_info(Square king_sq, Color perspective) {
    PerspectiveInfo info;
    int ksq = int(king_sq);
    // For black perspective, flip vertically
    if (perspective == BLACK) ksq ^= 56;
    // Check and apply horizontal mirror if king on files e-h
    info.mirrored = needs_mirror(ksq);
    info.king_oriented = info.mirrored ? mirror_square(ksq) : ksq;
    return info;
}

// Compute the feature index for a piece from a given perspective.
//   info       : pre-computed king orientation info
//   perspective: WHITE=0, BLACK=1
//   piece_color: actual color of the piece on the board
//   pt         : piece type (PAWN..QUEEN, 1..5; must NOT be KING=6)
//   sq         : actual square of the piece on the board (0..63)
inline int compute_feature_index(const PerspectiveInfo& info,
                                 Color perspective, Color piece_color,
                                 PieceType pt, Square sq) {
    int rel_color = (piece_color == perspective) ? 0 : 1;
    int pt_0based = int(pt) - 1;  // PAWN=1→0 .. QUEEN=5→4
    int oriented_sq = int(sq);
    // For black perspective, flip vertically
    if (perspective == BLACK) oriented_sq ^= 56;
    // Apply horizontal mirror if king is mirrored
    if (info.mirrored) oriented_sq ^= 7;
    return feature_index(info.king_oriented, rel_color, pt_0based, oriented_sq);
}

inline int orient_square_for_perspective(const PerspectiveInfo& info,
                                         Color perspective,
                                         Square sq) {
    int oriented_sq = int(sq);
    if (perspective == BLACK) oriented_sq ^= 56;
    if (info.mirrored) oriented_sq ^= 7;
    return oriented_sq;
}

// ---- Incremental primitives -----------------------------------------------

// Add a piece to ONE perspective of the accumulator (you must call per perspective)
void acc_add_piece_perspective(const Network& net, Accumulator& acc,
                               int perspective_idx, int feature_idx);

// Remove a piece from ONE perspective
void acc_sub_piece_perspective(const Network& net, Accumulator& acc,
                               int perspective_idx, int feature_idx);

// ---- High-level per-move update -------------------------------------------

// Called from Board::do_move after the base state has been copied.
// `acc` is the new state's accumulator (to be computed).
// `prev` is the parent state's accumulator (already computed).
// `m` is the move being made, `side` is the side that played it.
// `captured` is the captured piece (NO_PIECE if none).
//
// NOTE: If the move is a king move, this triggers a full refresh instead of
//       incremental update (because the king bucket changes).
void update_accumulator_do_move(const Network& net,
                                Accumulator& acc,
                                const Accumulator& prev,
                                const Board& board,
                                Move m, Color side, Piece captured);

// For null moves, just copy the parent accumulator.
void update_accumulator_null_move(Accumulator& acc, const Accumulator& prev);

// Full refresh (delegates to Network::refresh).
void update_accumulator_full(const Network& net, Accumulator& acc, const Board& board);

} // namespace Chess::NNUE
