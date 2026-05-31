#include "bitboard.h"
#include <iostream>
#include <iomanip>
#include <cstring>

namespace Chess {

// --- Global attack tables ---
Bitboard KNIGHT_ATTACKS[SQUARE_NB];
Bitboard KING_ATTACKS[SQUARE_NB];
Bitboard PAWN_ATTACKS[COLOR_NB][SQUARE_NB];
Bitboard BETWEEN_BB[SQUARE_NB][SQUARE_NB];
Bitboard LINE_BB[SQUARE_NB][SQUARE_NB];

// --- Magic bitboard tables ---
Magic BishopMagics[SQUARE_NB];
Magic RookMagics[SQUARE_NB];

// Attack table storage — bishop needs ~5.2KB, rook needs ~800KB
static Bitboard BishopAttackTable[0x1480]; // 5248 entries
static Bitboard RookAttackTable[0x19000]; // 102400 entries

// ============================================================================
// Slow reference sliding attack — used only during initialization
// ============================================================================

constexpr int BISHOP_DIRS_ARR[4] = { NORTH_EAST, NORTH_WEST, SOUTH_EAST, SOUTH_WEST };
constexpr int ROOK_DIRS_ARR[4]   = { NORTH, SOUTH, EAST, WEST };

Bitboard sliding_attack_slow(const int directions[4], Square s, Bitboard occupied) {
    Bitboard attacks = 0;
    for (int i = 0; i < 4; ++i) {
        int dir = directions[i];
        Square sq = s;
        while (true) {
            int next = int(sq) + dir;
            if (next < 0 || next >= 64) break;
            int file_diff = (next & 7) - (int(sq) & 7);
            if (file_diff > 2 || file_diff < -2) break;
            sq = Square(next);
            attacks |= square_bb(sq);
            if (occupied & square_bb(sq)) break;
        }
    }
    return attacks;
}

// ============================================================================
// Magic number tables — pre-computed magics for each square
// ============================================================================

namespace {

// Pre-computed magic numbers (from well-known sources)
constexpr Bitboard RookMagicNumbers[SQUARE_NB] = {
    0x0080001020400080ULL, 0x0040001000200040ULL, 0x0080081000200080ULL, 0x0080040800100080ULL,
    0x0080020400080080ULL, 0x0080010200040080ULL, 0x0080008001000200ULL, 0x0080002040800100ULL,
    0x0000800020400080ULL, 0x0000400020005000ULL, 0x0000801000200080ULL, 0x0000800800100080ULL,
    0x0000800400080080ULL, 0x0000800200040080ULL, 0x0000800100020080ULL, 0x0000800040800100ULL,
    0x0000208000400080ULL, 0x0000404000201000ULL, 0x0000808010002000ULL, 0x0000808008001000ULL,
    0x0000808004000800ULL, 0x0000808002000400ULL, 0x0000010100020004ULL, 0x0000020000408104ULL,
    0x0000208080004000ULL, 0x0000200040005000ULL, 0x0000100080200080ULL, 0x0000080080100080ULL,
    0x0000040080080080ULL, 0x0000020080040080ULL, 0x0000010080800200ULL, 0x0000800080004100ULL,
    0x0000204000800080ULL, 0x0000200040401000ULL, 0x0000100080802000ULL, 0x0000080080801000ULL,
    0x0000040080800800ULL, 0x0000020080800400ULL, 0x0000020001010004ULL, 0x0000800040800100ULL,
    0x0000204000808000ULL, 0x0000200040008080ULL, 0x0000100020008080ULL, 0x0000080010008080ULL,
    0x0000040008008080ULL, 0x0000020004008080ULL, 0x0000010002008080ULL, 0x0000004081020004ULL,
    0x0000204000800080ULL, 0x0000200040008080ULL, 0x0000100020008080ULL, 0x0000080010008080ULL,
    0x0000040008008080ULL, 0x0000020004008080ULL, 0x0000800100020080ULL, 0x0000800041000080ULL,
    0x00FFFCDDFCED714AULL, 0x007FFCDDFCED714AULL, 0x003FFFCDFFD88096ULL, 0x0000040810002101ULL,
    0x0001000204080011ULL, 0x0001000204000801ULL, 0x0001000082000401ULL, 0x0001FFFAABFAD1A2ULL,
};

constexpr Bitboard BishopMagicNumbers[SQUARE_NB] = {
    0x0002020202020200ULL, 0x0002020202020000ULL, 0x0004010202000000ULL, 0x0004040080000000ULL,
    0x0001104000000000ULL, 0x0000821040000000ULL, 0x0000410410400000ULL, 0x0000104104104000ULL,
    0x0000040404040400ULL, 0x0000020202020200ULL, 0x0000040102020000ULL, 0x0000040400800000ULL,
    0x0000011040000000ULL, 0x0000008210400000ULL, 0x0000004104104000ULL, 0x0000002082082000ULL,
    0x0004000808080800ULL, 0x0002000404040400ULL, 0x0001000202020200ULL, 0x0000800802004000ULL,
    0x0000800400A00000ULL, 0x0000200100884000ULL, 0x0000400082082000ULL, 0x0000200041041000ULL,
    0x0002080010101000ULL, 0x0001040008080800ULL, 0x0000208004010400ULL, 0x0000404004010200ULL,
    0x0000840000802000ULL, 0x0000404002011000ULL, 0x0000808001041000ULL, 0x0000404000820800ULL,
    0x0001041000202000ULL, 0x0000820800101000ULL, 0x0000104400080800ULL, 0x0000020080080080ULL,
    0x0000404040040100ULL, 0x0000808100020100ULL, 0x0001010100020800ULL, 0x0000808080010400ULL,
    0x0000820820004000ULL, 0x0000410410002000ULL, 0x0000082088001000ULL, 0x0000002011000800ULL,
    0x0000080100400400ULL, 0x0001010101000200ULL, 0x0002020202000400ULL, 0x0001010101000200ULL,
    0x0000410410400000ULL, 0x0000208208200000ULL, 0x0000002084100000ULL, 0x0000000020880000ULL,
    0x0000001002020000ULL, 0x0000040408020000ULL, 0x0004040404040000ULL, 0x0002020202020000ULL,
    0x0000104104104000ULL, 0x0000002082082000ULL, 0x0000000020841000ULL, 0x0000000000208800ULL,
    0x0000000010020200ULL, 0x0000000404080200ULL, 0x0000040404040400ULL, 0x0002020202020200ULL,
};

// Bit counts for each square's relevant occupancy mask
constexpr int RookBits[SQUARE_NB] = {
    12, 11, 11, 11, 11, 11, 11, 12,
    11, 10, 10, 10, 10, 10, 10, 11,
    11, 10, 10, 10, 10, 10, 10, 11,
    11, 10, 10, 10, 10, 10, 10, 11,
    11, 10, 10, 10, 10, 10, 10, 11,
    11, 10, 10, 10, 10, 10, 10, 11,
    11, 10, 10, 10, 10, 10, 10, 11,
    12, 11, 11, 11, 11, 11, 11, 12,
};

constexpr int BishopBits[SQUARE_NB] = {
    6, 5, 5, 5, 5, 5, 5, 6,
    5, 5, 5, 5, 5, 5, 5, 5,
    5, 5, 7, 7, 7, 7, 5, 5,
    5, 5, 7, 9, 9, 7, 5, 5,
    5, 5, 7, 9, 9, 7, 5, 5,
    5, 5, 7, 7, 7, 7, 5, 5,
    5, 5, 5, 5, 5, 5, 5, 5,
    6, 5, 5, 5, 5, 5, 5, 6,
};

// Compute the relevant occupancy mask for a square (excludes edges)
Bitboard compute_mask(Square s, bool is_rook) {
    Bitboard mask = 0;
    int r = rank_of(s), f = file_of(s);

    if (is_rook) {
        for (int i = r + 1; i < 7; ++i) mask |= square_bb(make_square(File(f), Rank(i)));
        for (int i = r - 1; i > 0; --i) mask |= square_bb(make_square(File(f), Rank(i)));
        for (int i = f + 1; i < 7; ++i) mask |= square_bb(make_square(File(i), Rank(r)));
        for (int i = f - 1; i > 0; --i) mask |= square_bb(make_square(File(i), Rank(r)));
    } else {
        for (int i = 1; r + i < 7 && f + i < 7; ++i) mask |= square_bb(make_square(File(f + i), Rank(r + i)));
        for (int i = 1; r + i < 7 && f - i > 0; ++i) mask |= square_bb(make_square(File(f - i), Rank(r + i)));
        for (int i = 1; r - i > 0 && f + i < 7; ++i) mask |= square_bb(make_square(File(f + i), Rank(r - i)));
        for (int i = 1; r - i > 0 && f - i > 0; ++i) mask |= square_bb(make_square(File(f - i), Rank(r - i)));
    }
    return mask;
}

// Enumerate occupancy subset from index
Bitboard index_to_occupancy(int index, int bits, Bitboard mask) {
    Bitboard occ = 0;
    for (int i = 0; i < bits; ++i) {
        Square s = pop_lsb(mask);
        if (index & (1 << i))
            occ |= square_bb(s);
    }
    return occ;
}

void init_magics(Magic magics[], Bitboard table[], const Bitboard magic_numbers[],
                 const int bits[], bool is_rook) {
    const int* dirs = is_rook ? ROOK_DIRS_ARR : BISHOP_DIRS_ARR;
    Bitboard* current = table;

    for (Square s = A1; s < Square(SQUARE_NB); ++s) {
        Magic& m = magics[s];
        m.mask    = compute_mask(s, is_rook);
        m.magic   = magic_numbers[s];
        m.shift   = 64 - bits[s];
        m.attacks = current;

        int num_entries = 1 << bits[s];

        // Fill the attack table for every possible occupancy subset
        for (int i = 0; i < num_entries; ++i) {
            Bitboard mask_copy = m.mask;
            Bitboard occ = index_to_occupancy(i, bits[s], mask_copy);
            unsigned idx = m.index(occ);
            m.attacks[idx] = sliding_attack_slow(dirs, s, occ);
        }

        current += num_entries;
    }
}

void init_knight_attacks() {
    static constexpr int jumps[8][2] = {
        {-2,-1},{-2,1},{-1,-2},{-1,2},{1,-2},{1,2},{2,-1},{2,1}
    };
    for (Square s = A1; s < Square(SQUARE_NB); ++s) {
        KNIGHT_ATTACKS[s] = 0;
        int f = file_of(s);
        int r = rank_of(s);
        for (auto [df, dr] : jumps) {
            int nf = f + df, nr = r + dr;
            if (nf >= 0 && nf < 8 && nr >= 0 && nr < 8) {
                KNIGHT_ATTACKS[s] |= square_bb(make_square(File(nf), Rank(nr)));
            }
        }
    }
}

void init_king_attacks() {
    for (Square s = A1; s < Square(SQUARE_NB); ++s) {
        Bitboard b = square_bb(s);
        KING_ATTACKS[s] =
            shift<NORTH>(b) | shift<SOUTH>(b) |
            shift<EAST>(b)  | shift<WEST>(b)  |
            shift<NORTH_EAST>(b) | shift<NORTH_WEST>(b) |
            shift<SOUTH_EAST>(b) | shift<SOUTH_WEST>(b);
    }
}

void init_pawn_attacks() {
    for (Square s = A1; s < Square(SQUARE_NB); ++s) {
        Bitboard b = square_bb(s);
        PAWN_ATTACKS[WHITE][s] = shift<NORTH_EAST>(b) | shift<NORTH_WEST>(b);
        PAWN_ATTACKS[BLACK][s] = shift<SOUTH_EAST>(b) | shift<SOUTH_WEST>(b);
    }
}

void init_between_and_lines() {
    for (Square s1 = A1; s1 < Square(SQUARE_NB); ++s1) {
        for (Square s2 = A1; s2 < Square(SQUARE_NB); ++s2) {
            BETWEEN_BB[s1][s2] = 0;
            LINE_BB[s1][s2] = 0;

            if (s1 == s2) continue;

            Bitboard sqs = square_bb(s1) | square_bb(s2);

            if (rook_attacks(s1, 0) & square_bb(s2)) {
                LINE_BB[s1][s2] = (rook_attacks(s1, 0) & rook_attacks(s2, 0)) | sqs;
                BETWEEN_BB[s1][s2] = rook_attacks(s1, square_bb(s2)) &
                                     rook_attacks(s2, square_bb(s1));
            }
            if (bishop_attacks(s1, 0) & square_bb(s2)) {
                LINE_BB[s1][s2] = (bishop_attacks(s1, 0) & bishop_attacks(s2, 0)) | sqs;
                BETWEEN_BB[s1][s2] = bishop_attacks(s1, square_bb(s2)) &
                                     bishop_attacks(s2, square_bb(s1));
            }
        }
    }
}

} // anonymous namespace

// ============================================================================
// Initialization
// ============================================================================

void bitboards_init() {
    init_knight_attacks();
    init_king_attacks();
    init_pawn_attacks();

    // Initialize magic bitboard tables
    init_magics(BishopMagics, BishopAttackTable, BishopMagicNumbers, BishopBits, false);
    init_magics(RookMagics,   RookAttackTable,   RookMagicNumbers,   RookBits,   true);

    // BETWEEN and LINE depend on working sliding attacks, init after magics
    init_between_and_lines();
}

// ============================================================================
// Debug printing
// ============================================================================

void print_bitboard(Bitboard b) {
    std::cout << "\n +---+---+---+---+---+---+---+---+\n";
    for (Rank r = RANK_8; r >= RANK_1; r = Rank(int(r) - 1)) {
        std::cout << " |";
        for (File f = FILE_A; f < FILE_NB; ++f) {
            Square s = make_square(f, r);
            std::cout << ((b & square_bb(s)) ? " X |" : "   |");
        }
        std::cout << " " << (1 + int(r)) << "\n +---+---+---+---+---+---+---+---+\n";
    }
    std::cout << "   a   b   c   d   e   f   g   h\n\n";
    std::cout << "  Hex: 0x" << std::hex << std::setfill('0') << std::setw(16)
              << b << std::dec << "\n\n";
}

} // namespace Chess
