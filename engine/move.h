#pragma once

#include "types.h"
#include <cstdint>

// ============================================================================
// Move encoding: 16-bit move representation
//
//  bits  0-5:  destination square (0-63)
//  bits  6-11: origin square (0-63)
//  bits 12-13: promotion piece type (0=Knight, 1=Bishop, 2=Rook, 3=Queen)
//  bits 14-15: special move flag (0=normal, 1=promotion, 2=en passant, 3=castling)
// ============================================================================

namespace Chess {

class Move {
public:
    constexpr Move() : data_(0) {}

    constexpr explicit Move(uint16_t d) : data_(d) {}

    constexpr Move(Square from, Square to, MoveType type = NORMAL, PieceType promo = KNIGHT)
        : data_(uint16_t(to) | (uint16_t(from) << 6) |
                uint16_t(type) |
                (type == PROMOTION ? uint16_t((promo - KNIGHT) << 12) : 0)) {}

    Square from() const { return Square((data_ >> 6) & 0x3F); }
    Square to()   const { return Square(data_ & 0x3F); }

    MoveType type() const { return MoveType(data_ & (3 << 14)); }

    PieceType promotion_type() const {
        return PieceType(((data_ >> 12) & 3) + KNIGHT);
    }

    uint16_t raw() const { return data_; }

    bool operator==(Move other) const { return data_ == other.data_; }
    bool operator!=(Move other) const { return data_ != other.data_; }

    explicit operator bool() const { return data_ != 0; }

    // Convert to UCI string (e.g., "e2e4", "e7e8q")
    std::string to_uci() const {
        if (!data_) return "(none)";
        std::string s = square_to_string(from()) + square_to_string(to());
        if (type() == PROMOTION) {
            s += "nbrq"[promotion_type() - KNIGHT];
        }
        return s;
    }

    // Parse from UCI string
    static Move from_uci(const std::string& s) {
        if (s.length() < 4) return Move();
        Square from = string_to_square(s.substr(0, 2));
        Square to   = string_to_square(s.substr(2, 2));

        MoveType mt = NORMAL;
        PieceType promo = KNIGHT;

        if (s.length() == 5) {
            mt = PROMOTION;
            switch (s[4]) {
                case 'n': promo = KNIGHT; break;
                case 'b': promo = BISHOP; break;
                case 'r': promo = ROOK;   break;
                case 'q': promo = QUEEN;  break;
                default:  break;
            }
        }
        return Move(from, to, mt, promo);
    }

    static constexpr Move none() { return Move(); }

private:
    uint16_t data_;
};

// Scored move for move ordering
struct ScoredMove {
    Move move;
    int  score = 0;
};

} // namespace Chess
