#pragma once

#include "types.h"
#include <bit>

// ============================================================================
// Bitboard utilities - the foundation of efficient move generation
// Now uses magic bitboards for O(1) sliding piece attack lookups.
// ============================================================================

namespace Chess {

// --- Constant bitboards ---
constexpr Bitboard FILE_A_BB = 0x0101010101010101ULL;
constexpr Bitboard FILE_B_BB = FILE_A_BB << 1;
constexpr Bitboard FILE_C_BB = FILE_A_BB << 2;
constexpr Bitboard FILE_D_BB = FILE_A_BB << 3;
constexpr Bitboard FILE_E_BB = FILE_A_BB << 4;
constexpr Bitboard FILE_F_BB = FILE_A_BB << 5;
constexpr Bitboard FILE_G_BB = FILE_A_BB << 6;
constexpr Bitboard FILE_H_BB = FILE_A_BB << 7;

constexpr Bitboard RANK_1_BB = 0xFFULL;
constexpr Bitboard RANK_2_BB = RANK_1_BB << 8;
constexpr Bitboard RANK_3_BB = RANK_1_BB << 16;
constexpr Bitboard RANK_4_BB = RANK_1_BB << 24;
constexpr Bitboard RANK_5_BB = RANK_1_BB << 32;
constexpr Bitboard RANK_6_BB = RANK_1_BB << 40;
constexpr Bitboard RANK_7_BB = RANK_1_BB << 48;
constexpr Bitboard RANK_8_BB = RANK_1_BB << 56;

constexpr Bitboard ALL_SQUARES = ~Bitboard(0);
constexpr Bitboard DARK_SQUARES = 0xAA55AA55AA55AA55ULL;
constexpr Bitboard LIGHT_SQUARES = ~DARK_SQUARES;

// File and rank bitboard arrays for lookup
constexpr Bitboard FILE_BB[FILE_NB] = {
    FILE_A_BB, FILE_B_BB, FILE_C_BB, FILE_D_BB,
    FILE_E_BB, FILE_F_BB, FILE_G_BB, FILE_H_BB
};

constexpr Bitboard RANK_BB[RANK_NB] = {
    RANK_1_BB, RANK_2_BB, RANK_3_BB, RANK_4_BB,
    RANK_5_BB, RANK_6_BB, RANK_7_BB, RANK_8_BB
};

// --- Bit operations ---
constexpr Bitboard square_bb(Square s) {
    return Bitboard(1) << s;
}

// Population count (number of set bits)
inline int popcount(Bitboard b) {
    return std::popcount(b);
}

// Least significant bit index
inline Square lsb(Bitboard b) {
    assert(b);
    return Square(std::countr_zero(b));
}

// Most significant bit index
inline Square msb(Bitboard b) {
    assert(b);
    return Square(63 - std::countl_zero(b));
}

// Pop least significant bit and return its index
inline Square pop_lsb(Bitboard& b) {
    assert(b);
    Square s = lsb(b);
    b &= b - 1;
    return s;
}

// Check if more than one bit is set
constexpr bool more_than_one(Bitboard b) {
    return b & (b - 1);
}

// --- Shift operations (with wrapping protection) ---
template<int D>
constexpr Bitboard shift(Bitboard b) {
    if constexpr (D == NORTH)      return b << 8;
    if constexpr (D == SOUTH)      return b >> 8;
    if constexpr (D == EAST)       return (b & ~FILE_H_BB) << 1;
    if constexpr (D == WEST)       return (b & ~FILE_A_BB) >> 1;
    if constexpr (D == NORTH_EAST) return (b & ~FILE_H_BB) << 9;
    if constexpr (D == NORTH_WEST) return (b & ~FILE_A_BB) << 7;
    if constexpr (D == SOUTH_EAST) return (b & ~FILE_H_BB) >> 7;
    if constexpr (D == SOUTH_WEST) return (b & ~FILE_A_BB) >> 9;
    return 0;
}

// --- Precomputed attack tables (initialized at startup) ---
// Knight and King attacks are fixed patterns
extern Bitboard KNIGHT_ATTACKS[SQUARE_NB];
extern Bitboard KING_ATTACKS[SQUARE_NB];
extern Bitboard PAWN_ATTACKS[COLOR_NB][SQUARE_NB];

// For rays between squares, line arrays, etc.
extern Bitboard BETWEEN_BB[SQUARE_NB][SQUARE_NB];  // Squares strictly between two squares
extern Bitboard LINE_BB[SQUARE_NB][SQUARE_NB];      // Full line through two squares

// ============================================================================
// Magic bitboard structures for O(1) sliding piece attack lookups
// ============================================================================

struct Magic {
    Bitboard  mask;     // Relevant occupancy mask (excludes edges)
    Bitboard  magic;    // Magic number for this square
    Bitboard* attacks;  // Pointer into the attack table
    int       shift;    // 64 - popcount(mask)

    // Index into attack table given occupancy
    unsigned index(Bitboard occupied) const {
        return unsigned(((occupied & mask) * magic) >> shift);
    }
};

extern Magic BishopMagics[SQUARE_NB];
extern Magic RookMagics[SQUARE_NB];

// Attack functions — now O(1) table lookups
inline Bitboard bishop_attacks(Square s, Bitboard occupied) {
    return BishopMagics[s].attacks[BishopMagics[s].index(occupied)];
}

inline Bitboard rook_attacks(Square s, Bitboard occupied) {
    return RookMagics[s].attacks[RookMagics[s].index(occupied)];
}

inline Bitboard queen_attacks(Square s, Bitboard occupied) {
    return bishop_attacks(s, occupied) | rook_attacks(s, occupied);
}

inline Bitboard knight_attacks(Square s) { return KNIGHT_ATTACKS[s]; }
inline Bitboard king_attacks(Square s) { return KING_ATTACKS[s]; }
inline Bitboard pawn_attacks(Color c, Square s) { return PAWN_ATTACKS[c][s]; }

// Attacks by piece type
inline Bitboard attacks_bb(PieceType pt, Square s, Bitboard occupied) {
    switch (pt) {
        case KNIGHT: return knight_attacks(s);
        case BISHOP: return bishop_attacks(s, occupied);
        case ROOK:   return rook_attacks(s, occupied);
        case QUEEN:  return queen_attacks(s, occupied);
        case KING:   return king_attacks(s);
        default:     return 0;
    }
}

// Slow reference sliding attack (used during init only)
Bitboard sliding_attack_slow(const int directions[4], Square s, Bitboard occupied);

// Initialize all precomputed bitboard tables (including magic bitboards)
void bitboards_init();

// Debug: print a bitboard to stdout
void print_bitboard(Bitboard b);

// --- Adjacent file masks (used for isolated/passed pawn detection) ---
constexpr Bitboard adjacent_files_bb(File f) {
    Bitboard adj = 0;
    if (f > FILE_A) adj |= FILE_BB[f - 1];
    if (f < FILE_H) adj |= FILE_BB[f + 1];
    return adj;
}

constexpr Bitboard ADJACENT_FILES_BB[FILE_NB] = {
    adjacent_files_bb(FILE_A), adjacent_files_bb(FILE_B),
    adjacent_files_bb(FILE_C), adjacent_files_bb(FILE_D),
    adjacent_files_bb(FILE_E), adjacent_files_bb(FILE_F),
    adjacent_files_bb(FILE_G), adjacent_files_bb(FILE_H)
};

// Passed pawn mask: all squares in front of the pawn on the same and adjacent files.
// For white pawns: ranks above the pawn's rank; for black: ranks below.
inline Bitboard passed_pawn_mask(Color c, Square s) {
    File f = file_of(s);
    Rank r = rank_of(s);
    Bitboard mask = FILE_BB[f] | ADJACENT_FILES_BB[f];
    Bitboard front = 0;
    if (c == WHITE) {
        for (Rank rr = Rank(r + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
            front |= RANK_BB[rr];
    } else {
        for (Rank rr = Rank(r - 1); rr >= RANK_1; rr = Rank(int(rr) - 1))
            front |= RANK_BB[rr];
    }
    return mask & front;
}

} // namespace Chess
