#pragma once

#include "types.h"
#include "bitboard.h"
#include "move.h"
#include "nnue/nnue_accumulator.h"
#include <string>
#include <array>

// ============================================================================
// Board representation using bitboards
// Maintains 12 bitboards (one per piece type per color), plus auxiliary data.
// ============================================================================

namespace Chess {

// State that is hard to undo — stored on a stack and restored on unmake_move
struct StateInfo {
    // Copied from previous state
    CastlingRight castling   = ALL_CASTLING;
    Square        ep_square  = NO_SQUARE;
    int           halfmove   = 0;
    int           fullmove   = 1;

    // Computed
    Key           key        = 0;  // Zobrist hash
    Key           pawn_key   = 0;  // Pawn-structure hash (incremental)
    Piece         captured   = NO_PIECE;
    Bitboard      checkers   = 0;

    // NNUE accumulator — maintained incrementally by do_move / undo_move
    NNUE::Accumulator nnue_acc;

    StateInfo* previous = nullptr;
};

class Board {
public:
    Board();
    Board(const Board& other);
    Board& operator=(const Board& other);

    // --- Setup ---
    void set_startpos();
    void set_fen(const std::string& fen);
    std::string to_fen() const;

    // --- Move execution ---
    void do_move(Move m, StateInfo& new_state);
    void undo_move(Move m);
    void do_null_move(StateInfo& new_state);
    void undo_null_move();

    // --- Query ---
    Color     side_to_move()  const { return side_; }
    Piece     piece_on(Square s) const { return board_[s]; }
    Square    king_square(Color c) const { return king_sq_[c]; }
    Bitboard  pieces() const { return by_color_[WHITE] | by_color_[BLACK]; }
    Bitboard  pieces(Color c) const { return by_color_[c]; }
    Bitboard  pieces(PieceType pt) const { return by_type_[pt]; }
    Bitboard  pieces(Color c, PieceType pt) const { return by_color_[c] & by_type_[pt]; }
    Bitboard  pieces(PieceType pt1, PieceType pt2) const { return by_type_[pt1] | by_type_[pt2]; }
    Bitboard  pieces(Color c, PieceType pt1, PieceType pt2) const {
        return by_color_[c] & (by_type_[pt1] | by_type_[pt2]);
    }

    CastlingRight castling_rights() const { return state_->castling; }
    Square        ep_square()     const { return state_->ep_square; }
    int           halfmove()      const { return state_->halfmove; }
    int           fullmove()      const { return state_->fullmove; }
    Key           key()           const { return state_->key; }
    Bitboard      checkers()      const { return state_->checkers; }
    int           ply()           const { return ply_; }

    // --- Attack queries ---
    Bitboard attackers_to(Square s, Bitboard occupied) const;
    Bitboard attackers_to(Square s) const { return attackers_to(s, pieces()); }
    bool     is_attacked(Square s, Color by) const;
    bool     in_check()  const { return state_->checkers != 0; }

    // --- Legality ---
    bool is_legal(Move m) const;
    bool gives_check(Move m) const;

    // --- Helpers ---
    bool is_capture(Move m) const { return piece_on(m.to()) != NO_PIECE || m.type() == EN_PASSANT; }
    bool is_draw() const; // 50-move, insufficient material, repetition

    // Non-pawn material for a given side (used for null move pruning decisions)
    int non_pawn_material(Color c) const;

    // Static Exchange Evaluation: returns true if the SEE of the move >= threshold
    // Used for pruning bad captures and ordering
    bool see_ge(Move m, int threshold) const;

    // Pawn-structure hash — maintained incrementally, O(1) lookup
    Key pawn_key() const { return state_->pawn_key; }

    // --- NNUE accumulator access ---
    NNUE::Accumulator& nnue_accumulator() const { return state_->nnue_acc; }

    // --- Internal state access (for SMP save/restore) ---
    StateInfo* state_ptr() const { return state_; }
    void set_state_ptr(StateInfo* s) { state_ = s; }
    int  internal_ply() const { return ply_; }
    void set_internal_ply(int p) { ply_ = p; }

    // --- Debug ---
    void print() const;
    bool is_valid() const;  // Consistency check

    // Zobrist hashing
    static void init_zobrist();

private:
    // Piece placement
    Piece     board_[SQUARE_NB];
    Bitboard  by_type_[PIECE_TYPE_NB];
    Bitboard  by_color_[COLOR_NB];
    Square    king_sq_[COLOR_NB];

    // Game state
    Color      side_;
    int        ply_;
    StateInfo* state_;
    StateInfo  root_state_;  // Owned state for FEN / startpos setup

    // Internal helpers
    void put_piece(Piece p, Square s);
    void remove_piece(Square s);
    void move_piece(Square from, Square to);
    void compute_checkers();
    Key compute_key() const;
    Key compute_pawn_key() const;

    // Zobrist tables
    static Key ZOBRIST_PSQ[PIECE_NB][SQUARE_NB];
    static Key ZOBRIST_EP[FILE_NB];
    static Key ZOBRIST_CASTLING[CASTLING_RIGHT_NB];
    static Key ZOBRIST_SIDE;
    static bool zobrist_initialized_;
};

} // namespace Chess
