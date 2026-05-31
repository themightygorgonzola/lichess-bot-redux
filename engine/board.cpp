#include "board.h"
#include "nnue/nnue_updater.h"
#include "nnue/nnue_network.h"
#include <iostream>
#include <sstream>
#include <random>
#include <cstring>

namespace Chess {

// --- Zobrist tables ---
Key Board::ZOBRIST_PSQ[PIECE_NB][SQUARE_NB];
Key Board::ZOBRIST_EP[FILE_NB];
Key Board::ZOBRIST_CASTLING[CASTLING_RIGHT_NB];
Key Board::ZOBRIST_SIDE;
bool Board::zobrist_initialized_ = false;

void Board::init_zobrist() {
    if (zobrist_initialized_) return;

    std::mt19937_64 rng(0xBEEF1234CAFE5678ULL); // Fixed seed for reproducibility

    for (int p = 0; p < PIECE_NB; ++p)
        for (int s = 0; s < SQUARE_NB; ++s)
            ZOBRIST_PSQ[p][s] = rng();

    for (int f = 0; f < FILE_NB; ++f)
        ZOBRIST_EP[f] = rng();

    for (int cr = 0; cr < CASTLING_RIGHT_NB; ++cr)
        ZOBRIST_CASTLING[cr] = rng();

    ZOBRIST_SIDE = rng();
    zobrist_initialized_ = true;
}

Board::Board() : side_(WHITE), ply_(0), state_(nullptr) {
    std::memset(board_, 0, sizeof(board_));
    std::memset(by_type_, 0, sizeof(by_type_));
    std::memset(by_color_, 0, sizeof(by_color_));
    king_sq_[WHITE] = king_sq_[BLACK] = NO_SQUARE;
}

Board::Board(const Board& other) {
    // Copy all board data
    std::memcpy(board_, other.board_, sizeof(board_));
    std::memcpy(by_type_, other.by_type_, sizeof(by_type_));
    std::memcpy(by_color_, other.by_color_, sizeof(by_color_));
    std::memcpy(king_sq_, other.king_sq_, sizeof(king_sq_));
    side_ = other.side_;
    ply_ = 0;  // Reset ply for new thread
    
    // Deep copy current state into our root_state_, preserving the
    // game-history chain via the previous pointer so that is_draw() can
    // detect repetitions of positions from earlier in the game.
    // The search never calls undo_move() past the root, so it will never
    // traverse into game-history states — only reads them for key comparison.
    if (other.state_) {
        root_state_ = *other.state_;
    }
    state_ = &root_state_;
    // previous is deliberately kept (not nullptr) so the full game history
    // is visible during repetition detection inside the search tree.
}

Board& Board::operator=(const Board& other) {
    if (this != &other) {
        std::memcpy(board_, other.board_, sizeof(board_));
        std::memcpy(by_type_, other.by_type_, sizeof(by_type_));
        std::memcpy(by_color_, other.by_color_, sizeof(by_color_));
        std::memcpy(king_sq_, other.king_sq_, sizeof(king_sq_));
        side_ = other.side_;
        ply_ = 0;
        
        if (other.state_) {
            root_state_ = *other.state_;
        }
        state_ = &root_state_;
        // Preserve previous chain for game-history repetition detection.
    }
    return *this;
}

void Board::put_piece(Piece p, Square s) {
    board_[s] = p;
    by_type_[type_of(p)] |= square_bb(s);
    by_color_[color_of(p)] |= square_bb(s);
    if (type_of(p) == KING)
        king_sq_[color_of(p)] = s;
}

void Board::remove_piece(Square s) {
    Piece p = board_[s];
    by_type_[type_of(p)] ^= square_bb(s);
    by_color_[color_of(p)] ^= square_bb(s);
    board_[s] = NO_PIECE;
}

void Board::move_piece(Square from, Square to) {
    Piece p = board_[from];
    Bitboard move_bb = square_bb(from) | square_bb(to);
    by_type_[type_of(p)] ^= move_bb;
    by_color_[color_of(p)] ^= move_bb;
    board_[from] = NO_PIECE;
    board_[to] = p;
    if (type_of(p) == KING)
        king_sq_[color_of(p)] = to;
}

void Board::set_startpos() {
    set_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
}

void Board::set_fen(const std::string& fen) {
    std::memset(board_, 0, sizeof(board_));
    std::memset(by_type_, 0, sizeof(by_type_));
    std::memset(by_color_, 0, sizeof(by_color_));
    king_sq_[WHITE] = king_sq_[BLACK] = NO_SQUARE;
    ply_ = 0;

    std::istringstream ss(fen);
    std::string token;

    // 1. Piece placement
    ss >> token;
    Square s = A8;
    for (char c : token) {
        if (c == '/') {
            s = Square(int(s) - 16); // Go to the start of the next rank down
        } else if (c >= '1' && c <= '8') {
            s = Square(int(s) + (c - '0'));
        } else {
            Piece p = NO_PIECE;
            switch (c) {
                case 'P': p = W_PAWN;   break; case 'N': p = W_KNIGHT; break;
                case 'B': p = W_BISHOP; break; case 'R': p = W_ROOK;   break;
                case 'Q': p = W_QUEEN;  break; case 'K': p = W_KING;   break;
                case 'p': p = B_PAWN;   break; case 'n': p = B_KNIGHT; break;
                case 'b': p = B_BISHOP; break; case 'r': p = B_ROOK;   break;
                case 'q': p = B_QUEEN;  break; case 'k': p = B_KING;   break;
                default: break;
            }
            if (p != NO_PIECE) put_piece(p, s);
            ++s;
        }
    }

    // 2. Side to move
    ss >> token;
    side_ = (token == "w") ? WHITE : BLACK;

    // 3. Castling rights — stored in the board's own root_state_
    root_state_ = StateInfo{};
    state_ = &root_state_;
    state_->castling = NO_CASTLING;
    state_->previous = nullptr;

    ss >> token;
    for (char c : token) {
        switch (c) {
            case 'K': state_->castling |= WHITE_OO;  break;
            case 'Q': state_->castling |= WHITE_OOO; break;
            case 'k': state_->castling |= BLACK_OO;  break;
            case 'q': state_->castling |= BLACK_OOO; break;
            default: break;
        }
    }

    // 4. En passant
    ss >> token;
    if (token != "-" && token.length() == 2) {
        state_->ep_square = string_to_square(token);
    } else {
        state_->ep_square = NO_SQUARE;
    }

    // 5. Halfmove clock
    if (ss >> token) state_->halfmove = std::stoi(token);
    else state_->halfmove = 0;

    // 6. Fullmove number
    if (ss >> token) state_->fullmove = std::stoi(token);
    else state_->fullmove = 1;

    // Compute hash and checkers
    state_->key      = compute_key();
    state_->pawn_key = compute_pawn_key();
    compute_checkers();

    // NNUE: mark accumulator as needing a full refresh
    state_->nnue_acc.computed = false;
}

std::string Board::to_fen() const {
    std::ostringstream fen;

    // 1. Piece placement
    for (Rank r = RANK_8; r >= RANK_1; r = Rank(int(r) - 1)) {
        int empty = 0;
        for (File f = FILE_A; f < FILE_NB; ++f) {
            Piece p = board_[make_square(f, r)];
            if (p == NO_PIECE) {
                ++empty;
            } else {
                if (empty) { fen << empty; empty = 0; }
                fen << piece_to_char(p);
            }
        }
        if (empty) fen << empty;
        if (r > RANK_1) fen << '/';
    }

    // 2. Side to move
    fen << (side_ == WHITE ? " w " : " b ");

    // 3. Castling
    CastlingRight cr = state_->castling;
    if (cr == NO_CASTLING) fen << '-';
    else {
        if (cr & WHITE_OO)  fen << 'K';
        if (cr & WHITE_OOO) fen << 'Q';
        if (cr & BLACK_OO)  fen << 'k';
        if (cr & BLACK_OOO) fen << 'q';
    }

    // 4. En passant
    fen << ' ';
    if (state_->ep_square != NO_SQUARE)
        fen << square_to_string(state_->ep_square);
    else
        fen << '-';

    // 5-6. Halfmove and fullmove
    fen << ' ' << state_->halfmove << ' ' << state_->fullmove;

    return fen.str();
}

Key Board::compute_key() const {
    Key k = 0;
    for (Square s = A1; s < Square(SQUARE_NB); ++s) {
        Piece p = board_[s];
        if (p != NO_PIECE)
            k ^= ZOBRIST_PSQ[p][s];
    }
    if (state_->ep_square != NO_SQUARE)
        k ^= ZOBRIST_EP[file_of(state_->ep_square)];
    k ^= ZOBRIST_CASTLING[state_->castling];
    if (side_ == BLACK)
        k ^= ZOBRIST_SIDE;
    return k;
}

Key Board::compute_pawn_key() const {
    Key k = 0;
    Bitboard wp = pieces(WHITE, PAWN);
    while (wp) { Square s = pop_lsb(wp); k ^= ZOBRIST_PSQ[make_piece(WHITE, PAWN)][s]; }
    Bitboard bp = pieces(BLACK, PAWN);
    while (bp) { Square s = pop_lsb(bp); k ^= ZOBRIST_PSQ[make_piece(BLACK, PAWN)][s]; }
    return k;
}

void Board::compute_checkers() {
    Square ksq = king_sq_[side_];
    state_->checkers = attackers_to(ksq, pieces()) & pieces(~side_);
}

Bitboard Board::attackers_to(Square s, Bitboard occupied) const {
    return (pawn_attacks(WHITE, s) & pieces(BLACK, PAWN))
         | (pawn_attacks(BLACK, s) & pieces(WHITE, PAWN))
         | (knight_attacks(s)      & pieces(KNIGHT))
         | (bishop_attacks(s, occupied) & pieces(BISHOP, QUEEN))
         | (rook_attacks(s, occupied)   & pieces(ROOK, QUEEN))
         | (king_attacks(s)        & pieces(KING));
}

bool Board::is_attacked(Square s, Color by) const {
    return attackers_to(s, pieces()) & pieces(by);
}

// ============================================================================
// do_move / undo_move — core of the engine
// ============================================================================

void Board::do_move(Move m, StateInfo& new_state) {
    // Copy state
    new_state.castling = state_->castling;
    new_state.ep_square = NO_SQUARE;
    new_state.halfmove = state_->halfmove + 1;
    new_state.fullmove = state_->fullmove + (side_ == BLACK ? 1 : 0);
    new_state.captured = NO_PIECE;
    new_state.previous = state_;
    state_ = &new_state;

    Square from = m.from();
    Square to   = m.to();
    Piece  pc   = board_[from];
    Piece  captured = board_[to];

    assert(pc != NO_PIECE);
    assert(color_of(pc) == side_);

    // Handle castling
    if (m.type() == CASTLING) {
        // King move is from -> to, rook move determined by side
        Square rfrom, rto;
        if (to > from) { // Kingside
            rfrom = Square(from + 3);
            rto   = Square(from + 1);
        } else { // Queenside
            rfrom = Square(from - 4);
            rto   = Square(from - 1);
        }
        move_piece(from, to);   // King
        move_piece(rfrom, rto); // Rook
        state_->halfmove = state_->previous->halfmove + 1;
    }
    // Handle en passant
    else if (m.type() == EN_PASSANT) {
        Square capsq = Square(int(to) + (side_ == WHITE ? SOUTH : NORTH));
        state_->captured = board_[capsq];
        remove_piece(capsq);
        move_piece(from, to);
        state_->halfmove = 0;
    }
    // Handle promotion
    else if (m.type() == PROMOTION) {
        if (captured != NO_PIECE) {
            state_->captured = captured;
            remove_piece(to);
        }
        remove_piece(from);
        put_piece(make_piece(side_, m.promotion_type()), to);
        state_->halfmove = 0;
    }
    // Normal move
    else {
        if (captured != NO_PIECE) {
            state_->captured = captured;
            remove_piece(to);
            state_->halfmove = 0;
        }
        if (type_of(pc) == PAWN)
            state_->halfmove = 0;

        move_piece(from, to);

        // Double pawn push -> set en passant square
        if (type_of(pc) == PAWN && std::abs(int(to) - int(from)) == 16) {
            state_->ep_square = Square((int(from) + int(to)) / 2);
        }
    }

    // Update castling rights
    // Any move from or to a rook/king square removes that right
    static constexpr CastlingRight CASTLING_MASK[SQUARE_NB] = {
        // a1        b1             c1             d1             e1                 f1             g1             h1
        CastlingRight(~WHITE_OOO), ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        CastlingRight(~(WHITE_OO | WHITE_OOO)), ALL_CASTLING, ALL_CASTLING, CastlingRight(~WHITE_OO),
        // a2-h7: all castling preserved
        ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        // a8        b8             c8             d8             e8                 f8             g8             h8
        CastlingRight(~BLACK_OOO), ALL_CASTLING, ALL_CASTLING, ALL_CASTLING,
        CastlingRight(~(BLACK_OO | BLACK_OOO)), ALL_CASTLING, ALL_CASTLING, CastlingRight(~BLACK_OO),
    };
    state_->castling &= CASTLING_MASK[from];
    state_->castling &= CASTLING_MASK[to];

    // Switch side
    side_ = ~side_;
    ++ply_;

    // ── Incremental Zobrist key update ────────────────────────────────────────
    // Start from parent key and XOR only the deltas — O(1) instead of O(64).
    // `side_` is already flipped here; the moving side is ~side_.
    {
        Key k = state_->previous->key;
        Color moving_side = ~side_;

        // Always flip the side-to-move component
        k ^= ZOBRIST_SIDE;

        // Castling rights delta (common case: no change → branch-predicted away)
        if (state_->previous->castling != state_->castling) {
            k ^= ZOBRIST_CASTLING[state_->previous->castling];
            k ^= ZOBRIST_CASTLING[state_->castling];
        }

        // En passant file delta
        if (state_->previous->ep_square != NO_SQUARE)
            k ^= ZOBRIST_EP[file_of(state_->previous->ep_square)];
        if (state_->ep_square != NO_SQUARE)
            k ^= ZOBRIST_EP[file_of(state_->ep_square)];

        // Piece deltas — handled per move type
        if (m.type() == CASTLING) {
            Square rfrom, rto_sq;
            if (to > from) { rfrom = Square(from + 3); rto_sq = Square(from + 1); }
            else            { rfrom = Square(from - 4); rto_sq = Square(from - 1); }
            Piece rook = make_piece(moving_side, ROOK);
            k ^= ZOBRIST_PSQ[pc][from] ^ ZOBRIST_PSQ[pc][to];          // King
            k ^= ZOBRIST_PSQ[rook][rfrom] ^ ZOBRIST_PSQ[rook][rto_sq]; // Rook
        } else if (m.type() == EN_PASSANT) {
            Square capsq = Square(int(to) + (moving_side == WHITE ? SOUTH : NORTH));
            Piece cap_pawn = make_piece(~moving_side, PAWN);
            k ^= ZOBRIST_PSQ[pc][from] ^ ZOBRIST_PSQ[pc][to]; // Pawn
            k ^= ZOBRIST_PSQ[cap_pawn][capsq];                 // Captured pawn
        } else if (m.type() == PROMOTION) {
            Piece promo_piece = make_piece(moving_side, m.promotion_type());
            k ^= ZOBRIST_PSQ[pc][from];          // Remove pawn
            k ^= ZOBRIST_PSQ[promo_piece][to];   // Add promoted piece
            if (captured != NO_PIECE)
                k ^= ZOBRIST_PSQ[captured][to];  // Remove captured piece
        } else {
            k ^= ZOBRIST_PSQ[pc][from] ^ ZOBRIST_PSQ[pc][to]; // Piece moves
            if (captured != NO_PIECE)
                k ^= ZOBRIST_PSQ[captured][to];                // Captured piece
        }

        state_->key = k;
    }

    // ── Incremental pawn key update ───────────────────────────────────────────
    {
        Key pk = state_->previous->pawn_key;
        Color moving_side = ~side_; // side_ already flipped
        if (m.type() == EN_PASSANT) {
            // Moving pawn from->to; captured pawn at capsq
            Square capsq = Square(int(to) + (moving_side == WHITE ? SOUTH : NORTH));
            Piece cap_pawn = make_piece(~moving_side, PAWN);
            pk ^= ZOBRIST_PSQ[pc][from] ^ ZOBRIST_PSQ[pc][to];
            pk ^= ZOBRIST_PSQ[cap_pawn][capsq];
        } else if (m.type() == PROMOTION) {
            // Promote: pawn leaves, promoted piece is not a pawn
            pk ^= ZOBRIST_PSQ[pc][from];
            if (captured != NO_PIECE && type_of(captured) == PAWN)
                pk ^= ZOBRIST_PSQ[captured][to];
        } else {
            if (type_of(pc) == PAWN)
                pk ^= ZOBRIST_PSQ[pc][from] ^ ZOBRIST_PSQ[pc][to];
            if (captured != NO_PIECE && type_of(captured) == PAWN)
                pk ^= ZOBRIST_PSQ[captured][to];
        }
        state_->pawn_key = pk;
    }

    compute_checkers();

    // --- NNUE incremental accumulator update ---
    // The moving side is ~side_ (we already flipped).
    // The captured piece is in state_->captured.
    if (NNUE::g_network.is_loaded() && state_->previous &&
        state_->previous->nnue_acc.computed) {
        NNUE::update_accumulator_do_move(
            NNUE::g_network,
            state_->nnue_acc,
            state_->previous->nnue_acc,
            *this, m, ~side_, state_->captured);
    } else {
        state_->nnue_acc.computed = false;  // will be refreshed lazily
    }
}

void Board::undo_move(Move m) {
    side_ = ~side_;
    --ply_;

    Square from = m.from();
    Square to   = m.to();

    if (m.type() == CASTLING) {
        // Undo king move
        move_piece(to, from);
        // Undo rook move
        Square rfrom, rto;
        if (to > from) {
            rfrom = Square(from + 3);
            rto   = Square(from + 1);
        } else {
            rfrom = Square(from - 4);
            rto   = Square(from - 1);
        }
        move_piece(rto, rfrom);
    }
    else if (m.type() == PROMOTION) {
        remove_piece(to);
        put_piece(make_piece(side_, PAWN), from);
        if (state_->captured != NO_PIECE)
            put_piece(state_->captured, to);
    }
    else if (m.type() == EN_PASSANT) {
        move_piece(to, from);
        Square capsq = Square(int(to) + (side_ == WHITE ? SOUTH : NORTH));
        put_piece(state_->captured, capsq);
    }
    else {
        move_piece(to, from);
        if (state_->captured != NO_PIECE)
            put_piece(state_->captured, to);
    }

    state_ = state_->previous;
}

// ============================================================================
// Null move — pass the turn without moving a piece
// ============================================================================

void Board::do_null_move(StateInfo& new_state) {
    new_state.castling  = state_->castling;
    new_state.ep_square = NO_SQUARE;  // EP is always reset
    new_state.halfmove  = state_->halfmove + 1;
    new_state.fullmove  = state_->fullmove + (side_ == BLACK ? 1 : 0);
    new_state.captured  = NO_PIECE;
    new_state.previous  = state_;
    state_ = &new_state;

    side_ = ~side_;
    ++ply_;

    // Null move: no pieces change, just flip side and clear EP
    {
        Key k = state_->previous->key;
        k ^= ZOBRIST_SIDE;
        if (state_->previous->ep_square != NO_SQUARE)
            k ^= ZOBRIST_EP[file_of(state_->previous->ep_square)];
        state_->key = k;
    }
    state_->pawn_key = state_->previous->pawn_key;

    compute_checkers();

    // NNUE: null move doesn't change pieces, so just copy accumulator
    if (state_->previous->nnue_acc.computed) {
        NNUE::update_accumulator_null_move(state_->nnue_acc,
                                           state_->previous->nnue_acc);
    } else {
        state_->nnue_acc.computed = false;
    }
}

void Board::undo_null_move() {
    side_ = ~side_;
    --ply_;
    state_ = state_->previous;
}

// ============================================================================
// Non-pawn material (for null-move pruning guard)
// ============================================================================

int Board::non_pawn_material(Color c) const {
    return popcount(pieces(c, KNIGHT)) * KNIGHT_VALUE
         + popcount(pieces(c, BISHOP)) * BISHOP_VALUE
         + popcount(pieces(c, ROOK))   * ROOK_VALUE
         + popcount(pieces(c, QUEEN))  * QUEEN_VALUE;
}



// ============================================================================
// Static Exchange Evaluation (SEE)
// Returns true if the resulting exchange on move m scores >= threshold.
// Uses the "swap algorithm" — alternating attacker/defender captures on `to`.
// ============================================================================

namespace {
constexpr int SEE_VALUES[PIECE_TYPE_NB] = {
    0, 100, 320, 330, 500, 900, 20000
};
} // anonymous namespace

bool Board::see_ge(Move m, int threshold) const {
    if (m.type() == CASTLING || m.type() == PROMOTION)
        return threshold <= 0;  // Simplified: promotions/castles are OK

    Square from = m.from();
    Square to   = m.to();

    int swap = -threshold;

    // Initial gain: value of captured piece (or pawn for EP)
    if (m.type() == EN_PASSANT) {
        swap += SEE_VALUES[PAWN];
    } else {
        Piece victim = board_[to];
        if (victim == NO_PIECE) return threshold <= 0;  // Non-capture
        swap += SEE_VALUES[type_of(victim)];
    }

    // If we're already winning even without the piece we're moving, great
    if (swap < 0) return false;

    // Cost: value of the piece we just moved (we might lose it)
    Piece mover = board_[from];
    swap -= SEE_VALUES[type_of(mover)];

    // If we're still winning even if we lose the mover, return true early
    if (swap >= 0) return true;

    // Now simulate the capture sequence
    Bitboard occupied = (pieces() ^ square_bb(from)) | square_bb(to);
    if (m.type() == EN_PASSANT) {
        Square capsq = Square(int(to) + (side_ == WHITE ? SOUTH : NORTH));
        occupied ^= square_bb(capsq);
    }

    Bitboard attackers = attackers_to(to, occupied) & occupied;
    Color stm = ~side_;  // Next side to capture

    while (true) {
        Bitboard stm_attackers = attackers & pieces(stm);
        if (!stm_attackers) break;

        // Pick the least valuable attacker
        PieceType pt;
        for (pt = PAWN; pt <= KING; pt = PieceType(int(pt) + 1)) {
            if (stm_attackers & pieces(pt)) break;
        }

        // Remove this attacker from occupied and add discovered attackers
        Bitboard bb = stm_attackers & pieces(pt);
        Square att_sq = lsb(bb);
        occupied ^= square_bb(att_sq);

        // Discover new sliding attackers through the vacated square
        if (pt == PAWN || pt == BISHOP || pt == QUEEN)
            attackers |= bishop_attacks(to, occupied) & pieces(BISHOP, QUEEN);
        if (pt == ROOK || pt == QUEEN)
            attackers |= rook_attacks(to, occupied) & pieces(ROOK, QUEEN);
        attackers &= occupied;

        stm = ~stm;
        swap = -swap - SEE_VALUES[pt];

        // If the current side is losing the exchange even after capturing
        if (swap >= 0) {
            // King can't be captured if opponent still has attackers
            if (pt == KING && (attackers & pieces(stm)))
                stm = ~stm;  // Flip back — king capture is illegal
            break;
        }
    }

    return side_ != stm;  // Side to move in the original position wins
}

// ============================================================================
// Legality check
// ============================================================================

bool Board::is_legal(Move m) const {
    Square from = m.from();
    Square to   = m.to();
    Square ksq  = king_sq_[side_];

    // En passant: need to check for discovered check along the rank
    if (m.type() == EN_PASSANT) {
        Square capsq = Square(int(to) + (side_ == WHITE ? SOUTH : NORTH));
        Bitboard occupied = (pieces() ^ square_bb(from) ^ square_bb(capsq)) | square_bb(to);
        return !(rook_attacks(ksq, occupied) & pieces(~side_, ROOK, QUEEN))
            && !(bishop_attacks(ksq, occupied) & pieces(~side_, BISHOP, QUEEN));
    }

    // Castling: check that king doesn't pass through or land on attacked squares.
    // Use occupancy without the king so x-ray attacks through the king's
    // original square are properly detected.
    if (m.type() == CASTLING) {
        Bitboard occ = pieces() ^ square_bb(from);
        int dir = (to > from) ? 1 : -1;
        for (Square s = Square(int(from) + dir); ; s = Square(int(s) + dir)) {
            if (attackers_to(s, occ) & pieces(~side_)) return false;
            if (s == to) break;
        }
        return true;
    }

    // King move: remove king from occupancy so sliding pieces can
    // "see through" the king's old square (x-ray detection).
    if (type_of(board_[from]) == KING) {
        Bitboard occ = pieces() ^ square_bb(from);
        return !(attackers_to(to, occ) & pieces(~side_));
    }

    // Non-king move: check for discovered attacks on our king.
    // Use full attackers_to with the post-move occupancy, excluding any
    // captured piece on 'to' (it's gone after this move).
    Bitboard occ_after = (pieces() ^ square_bb(from)) | square_bb(to);
    return !(attackers_to(ksq, occ_after) & pieces(~side_) & ~square_bb(to));
}

bool Board::gives_check(Move m) const {
    // Quick check: does the destination attack the enemy king?
    Square to = m.to();
    Square ksq = king_sq_[~side_];
    Piece pc = board_[m.from()];
    PieceType pt = type_of(pc);

    if (m.type() == PROMOTION) pt = m.promotion_type();

    // Direct check
    if (attacks_bb(pt, to, pieces()) & square_bb(ksq))
        return true;

    // Discovered check: moving piece reveals an attack line
    Bitboard occupied = (pieces() ^ square_bb(m.from())) | square_bb(to);
    if ((rook_attacks(ksq, occupied) & pieces(side_, ROOK, QUEEN))
     || (bishop_attacks(ksq, occupied) & pieces(side_, BISHOP, QUEEN)))
        return true;

    return false;
}

bool Board::is_draw() const {
    // 50-move rule
    if (state_->halfmove >= 100) return true;

    // Insufficient material
    int total = popcount(pieces());
    if (total == 2) return true; // K vs K
    if (total == 3) {
        if (pieces(KNIGHT) || pieces(BISHOP)) return true; // K+N vs K or K+B vs K
    }

    // Repetition detection.
    //
    // Walk backwards through the state chain stepping by 2 plies at a time so
    // we only compare positions with the same side to move.  The Zobrist key
    // includes a side-to-move component, so keys at odd offsets can never match
    // — the old code stepped by 3 (1 + 2 inside the loop) and therefore
    // compared against the WRONG side every time, making repetition invisible.
    //
    // We only look back as far as the halfmove clock allows: any pawn push or
    // capture resets the irreversible-move counter, and no position before that
    // point can be repeated.
    //
    // Any single repetition is treated as a draw.  This is conservative when
    // winning (the engine will actively avoid repeating) and beneficial when
    // losing (the engine will seek a repetition draw).
    const StateInfo* st = state_;
    for (int i = 2; i <= state_->halfmove; i += 2) {
        if (!st->previous) break;
        st = st->previous;
        if (!st->previous) break;
        st = st->previous;
        if (st->key == state_->key) {
            return true;  // Position repeated — draw
        }
    }

    return false;
}

// ============================================================================
// Print
// ============================================================================

void Board::print() const {
    const char* sep = " +---+---+---+---+---+---+---+---+\n";

    std::cout << "\n" << sep;
    for (Rank r = RANK_8; r >= RANK_1; r = Rank(int(r) - 1)) {
        std::cout << " |";
        for (File f = FILE_A; f < FILE_NB; ++f) {
            Piece p = board_[make_square(f, r)];
            std::cout << ' ' << (p == NO_PIECE ? '.' : piece_to_char(p)) << " |";
        }
        std::cout << " " << (1 + int(r)) << "\n" << sep;
    }
    std::cout << "   a   b   c   d   e   f   g   h\n\n";
    std::cout << "FEN: " << to_fen() << "\n";
    std::cout << "Key: 0x" << std::hex << state_->key << std::dec << "\n";
    std::cout << "Checkers: " << popcount(state_->checkers) << "\n\n";
}

bool Board::is_valid() const {
    // Basic consistency checks
    if (popcount(pieces(WHITE, KING)) != 1) return false;
    if (popcount(pieces(BLACK, KING)) != 1) return false;
    if (king_sq_[WHITE] != lsb(pieces(WHITE, KING))) return false;
    if (king_sq_[BLACK] != lsb(pieces(BLACK, KING))) return false;
    if (pieces(WHITE) & pieces(BLACK)) return false;
    return true;
}

} // namespace Chess
