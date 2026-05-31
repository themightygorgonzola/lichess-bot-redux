#pragma once

#include <cstdint>
#include <string>
#include <cassert>
#include <array>
#include <algorithm>

// ============================================================================
// Fundamental types for the chess engine
// ============================================================================

namespace Chess {

// --- Bitboard: 64-bit integer, one bit per square ---
using Bitboard = uint64_t;
using Key      = uint64_t;  // Zobrist hash key

// --- Squares: a1=0, b1=1, ... h8=63 ---
enum Square : int {
    A1, B1, C1, D1, E1, F1, G1, H1,
    A2, B2, C2, D2, E2, F2, G2, H2,
    A3, B3, C3, D3, E3, F3, G3, H3,
    A4, B4, C4, D4, E4, F4, G4, H4,
    A5, B5, C5, D5, E5, F5, G5, H5,
    A6, B6, C6, D6, E6, F6, G6, H6,
    A7, B7, C7, D7, E7, F7, G7, H7,
    A8, B8, C8, D8, E8, F8, G8, H8,
    NO_SQUARE = 64,
    SQUARE_NB = 64
};

// --- Files (columns) and Ranks (rows) ---
enum File : int { FILE_A, FILE_B, FILE_C, FILE_D, FILE_E, FILE_F, FILE_G, FILE_H, FILE_NB };
enum Rank : int { RANK_1, RANK_2, RANK_3, RANK_4, RANK_5, RANK_6, RANK_7, RANK_8, RANK_NB };

// --- Colors ---
enum Color : int { WHITE, BLACK, COLOR_NB = 2 };

// --- Piece types ---
enum PieceType : int {
    NO_PIECE_TYPE = 0,
    PAWN = 1, KNIGHT = 2, BISHOP = 3, ROOK = 4, QUEEN = 5, KING = 6,
    PIECE_TYPE_NB = 7
};

// --- Pieces (color + type encoded) ---
enum Piece : int {
    NO_PIECE = 0,
    W_PAWN = 1, W_KNIGHT = 2, W_BISHOP = 3, W_ROOK = 4, W_QUEEN = 5, W_KING = 6,
    B_PAWN = 9, B_KNIGHT = 10, B_BISHOP = 11, B_ROOK = 12, B_QUEEN = 13, B_KING = 14,
    PIECE_NB = 16
};

// --- Castling rights (bitmask) ---
enum CastlingRight : int {
    NO_CASTLING  = 0,
    WHITE_OO     = 1,   // White kingside
    WHITE_OOO    = 2,   // White queenside
    BLACK_OO     = 4,   // Black kingside
    BLACK_OOO    = 8,   // Black queenside
    ALL_CASTLING = 15,
    CASTLING_RIGHT_NB = 16
};

// --- Move flags ---
enum MoveType : int {
    NORMAL     = 0,
    PROMOTION  = 1 << 14,
    EN_PASSANT = 2 << 14,
    CASTLING   = 3 << 14
};

// --- Scores ---
enum Value : int {
    VALUE_ZERO     = 0,
    VALUE_DRAW     = 0,
    VALUE_MATE     = 32000,
    VALUE_INFINITE = 32001,
    VALUE_NONE     = 32002,

    VALUE_MATE_IN_MAX_PLY  =  VALUE_MATE - 128,
    VALUE_MATED_IN_MAX_PLY = -VALUE_MATE + 128,

    // Tablebase win/loss scores — above all positional evals, below checkmate range.
    // Adjusted by ply so "TB win in fewer moves" is preferred over "TB win in more moves".
    VALUE_TB_WIN  =  VALUE_MATE - 200,   // 31800  (TB proven win)
    VALUE_TB_LOSS = -VALUE_MATE + 200,   // -31800 (TB proven loss)

    // Material values (centipawns)
    PAWN_VALUE   = 100,
    KNIGHT_VALUE = 320,
    BISHOP_VALUE = 330,
    ROOK_VALUE   = 500,
    QUEEN_VALUE  = 900
};

// --- Search depth ---
enum Depth : int {
    DEPTH_ZERO = 0,
    DEPTH_QS   = -1,  // Quiescence search
    DEPTH_NONE = -127
};

// Max game ply / search depth
constexpr int MAX_PLY   = 128;
constexpr int MAX_MOVES = 256;  // Max legal moves in any position

// ============================================================================
// Utility functions
// ============================================================================

constexpr Square make_square(File f, Rank r) {
    return Square((r << 3) + f);
}

constexpr File file_of(Square s) { return File(s & 7); }
constexpr Rank rank_of(Square s) { return Rank(s >> 3); }

constexpr Color operator~(Color c) { return Color(c ^ 1); }

constexpr Piece make_piece(Color c, PieceType pt) {
    return Piece((c << 3) | pt);
}

constexpr Color color_of(Piece p) {
    assert(p != NO_PIECE);
    return Color(p >> 3);
}

constexpr PieceType type_of(Piece p) {
    return PieceType(p & 7);
}

constexpr bool is_ok(Square s) { return s >= A1 && s <= H8; }

constexpr CastlingRight operator|(CastlingRight a, CastlingRight b) {
    return CastlingRight(int(a) | int(b));
}
constexpr CastlingRight operator&(CastlingRight a, CastlingRight b) {
    return CastlingRight(int(a) & int(b));
}
constexpr CastlingRight& operator|=(CastlingRight& a, CastlingRight b) {
    return a = a | b;
}
constexpr CastlingRight& operator&=(CastlingRight& a, CastlingRight b) {
    return a = a & b;
}
constexpr CastlingRight operator~(CastlingRight c) {
    return CastlingRight(~int(c) & 15);
}

// Increment operators for iteration
constexpr Square& operator++(Square& s) { return s = Square(int(s) + 1); }
constexpr File& operator++(File& f) { return f = File(int(f) + 1); }
constexpr Rank& operator++(Rank& r) { return r = Rank(int(r) + 1); }

// Square arithmetic
constexpr Square operator+(Square s, int d) { return Square(int(s) + d); }
constexpr Square operator-(Square s, int d) { return Square(int(s) - d); }

// Direction offsets
constexpr int NORTH = 8;
constexpr int SOUTH = -8;
constexpr int EAST  = 1;
constexpr int WEST  = -1;
constexpr int NORTH_EAST = NORTH + EAST;
constexpr int NORTH_WEST = NORTH + WEST;
constexpr int SOUTH_EAST = SOUTH + EAST;
constexpr int SOUTH_WEST = SOUTH + WEST;

// Piece character lookup
inline char piece_to_char(Piece p) {
    constexpr char chars[] = " PNBRQK  pnbrqk";
    return chars[p];
}

// Square to string (e.g., "e4")
inline std::string square_to_string(Square s) {
    return std::string{char('a' + file_of(s)), char('1' + rank_of(s))};
}

// String to square
inline Square string_to_square(const std::string& s) {
    return make_square(File(s[0] - 'a'), Rank(s[1] - '1'));
}

} // namespace Chess
