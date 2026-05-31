#include "movegen.h"
#include <iostream>

namespace Chess {

// ============================================================================
// Pseudo-legal move generation
// ============================================================================

namespace {

void generate_pawn_moves(const Board& board, MoveList& list, GenType type) {
    Color us = board.side_to_move();
    Color them = ~us;
    int push_dir = (us == WHITE) ? NORTH : SOUTH;
    Rank promo_rank = (us == WHITE) ? RANK_7 : RANK_2;
    Rank start_rank = (us == WHITE) ? RANK_2 : RANK_7;
    Bitboard enemies = board.pieces(them);

    Bitboard pawns = board.pieces(us, PAWN);

    while (pawns) {
        Square from = pop_lsb(pawns);
        bool on_promo = (rank_of(from) == promo_rank);
        bool on_start = (rank_of(from) == start_rank);

        // --- Captures ---
        if (type != QUIET_ONLY) {
            Bitboard attacks = pawn_attacks(us, from) & enemies;
            while (attacks) {
                Square to = pop_lsb(attacks);
                if (on_promo) {
                    list.add(Move(from, to, PROMOTION, QUEEN));
                    list.add(Move(from, to, PROMOTION, ROOK));
                    list.add(Move(from, to, PROMOTION, BISHOP));
                    list.add(Move(from, to, PROMOTION, KNIGHT));
                } else {
                    list.add(Move(from, to));
                }
            }

            // En passant
            if (board.ep_square() != NO_SQUARE) {
                if (pawn_attacks(us, from) & square_bb(board.ep_square())) {
                    list.add(Move(from, board.ep_square(), EN_PASSANT));
                }
            }
        }

        // --- Pushes ---
        if (type != CAPTURES_ONLY || on_promo) {
            Square one_push = Square(int(from) + push_dir);
            if (is_ok(one_push) && board.piece_on(one_push) == NO_PIECE) {
                if (on_promo) {
                    list.add(Move(from, one_push, PROMOTION, QUEEN));
                    if (type != CAPTURES_ONLY) {
                        list.add(Move(from, one_push, PROMOTION, ROOK));
                        list.add(Move(from, one_push, PROMOTION, BISHOP));
                        list.add(Move(from, one_push, PROMOTION, KNIGHT));
                    }
                } else {
                    if (type != CAPTURES_ONLY)
                        list.add(Move(from, one_push));

                    // Double push
                    if (on_start) {
                        Square two_push = Square(int(one_push) + push_dir);
                        if (is_ok(two_push) && board.piece_on(two_push) == NO_PIECE) {
                            if (type != CAPTURES_ONLY)
                                list.add(Move(from, two_push));
                        }
                    }
                }
            }
        }
    }
}

void generate_piece_moves(const Board& board, MoveList& list, GenType type, PieceType pt) {
    Color us = board.side_to_move();
    Bitboard ours = board.pieces(us);
    Bitboard target;

    if (type == CAPTURES_ONLY) target = board.pieces(~us);
    else if (type == QUIET_ONLY) target = ~board.pieces();
    else target = ~ours;

    Bitboard pieces = board.pieces(us, pt);
    Bitboard occupied = board.pieces();

    while (pieces) {
        Square from = pop_lsb(pieces);
        Bitboard attacks = attacks_bb(pt, from, occupied) & target;
        while (attacks) {
            Square to = pop_lsb(attacks);
            list.add(Move(from, to));
        }
    }
}

void generate_castling(const Board& board, MoveList& list) {
    Color us = board.side_to_move();
    if (board.in_check()) return;

    Bitboard occupied = board.pieces();
    Square ksq = board.king_square(us);

    // Only check castling rights and path clearance here.
    // The is_legal() function handles the "king must not pass through
    // or land on an attacked square" check with correct x-ray logic.
    auto can_castle = [&](CastlingRight cr, Square rook_sq, Square king_to) {
        if (!(board.castling_rights() & cr)) return;
        Bitboard between = BETWEEN_BB[ksq][rook_sq];
        if (between & occupied) return;
        list.add(Move(ksq, king_to, CASTLING));
    };

    if (us == WHITE) {
        can_castle(WHITE_OO,  H1, G1);
        can_castle(WHITE_OOO, A1, C1);
    } else {
        can_castle(BLACK_OO,  H8, G8);
        can_castle(BLACK_OOO, A8, C8);
    }
}

} // anonymous namespace

void generate_pseudo_legal(const Board& board, MoveList& list, GenType type) {
    list.count = 0;
    generate_pawn_moves(board, list, type);
    generate_piece_moves(board, list, type, KNIGHT);
    generate_piece_moves(board, list, type, BISHOP);
    generate_piece_moves(board, list, type, ROOK);
    generate_piece_moves(board, list, type, QUEEN);
    generate_piece_moves(board, list, type, KING);
    if (type != CAPTURES_ONLY) {
        generate_castling(board, list);
    }
}

void generate_legal(const Board& board, MoveList& list, GenType type) {
    MoveList pseudo;
    generate_pseudo_legal(board, pseudo, type);

    list.count = 0;

    // Fast-path legality: compute pinned pieces once for this position.
    // A piece is pinned if it's the sole blocker between our king and an enemy
    // slider on a rook/bishop ray.  Non-king, non-EP, non-pinned moves that are
    // generated while NOT in check are always legal — we can skip is_legal()
    // for them and save ~25-35% NPS vs. the naive per-move is_legal() loop.
    Color  us       = board.side_to_move();
    Square ksq      = board.king_square(us);
    bool   in_check = board.in_check();

    Bitboard pinned = 0;
    if (!in_check) {
        Bitboard occ     = board.pieces();
        Bitboard snipers = (rook_attacks(ksq, 0)   & board.pieces(~us, ROOK,   QUEEN))
                         | (bishop_attacks(ksq, 0) & board.pieces(~us, BISHOP, QUEEN));
        while (snipers) {
            Square   sniper  = pop_lsb(snipers);
            Bitboard between = BETWEEN_BB[ksq][sniper] & occ;
            // Exactly one piece between king and sniper → it is pinned
            if (between && !more_than_one(between))
                pinned |= between & board.pieces(us);
        }
    }

    for (int i = 0; i < pseudo.count; ++i) {
        Move     m  = pseudo[i].move;
        PieceType pt = type_of(board.piece_on(m.from()));

        // Full legality check required for:
        //   • king moves (including castling — pt == KING covers both)
        //   • en passant (horizontal pin not caught by the pinned mask)
        //   • pinned pieces (may be moving off the pin ray)
        //   • any move while in check (must resolve the check)
        if (in_check
         || m.type() == EN_PASSANT
         || pt == KING
         || (pinned & square_bb(m.from()))) {
            if (board.is_legal(m)) list.add(m);
        } else {
            list.add(m);  // Definitely legal
        }
    }
}

bool has_legal_moves(const Board& board) {
    MoveList pseudo;
    generate_pseudo_legal(board, pseudo);

    Color  us       = board.side_to_move();
    Square ksq      = board.king_square(us);
    bool   in_check = board.in_check();

    Bitboard pinned = 0;
    if (!in_check) {
        Bitboard occ     = board.pieces();
        Bitboard snipers = (rook_attacks(ksq, 0)   & board.pieces(~us, ROOK,   QUEEN))
                         | (bishop_attacks(ksq, 0) & board.pieces(~us, BISHOP, QUEEN));
        while (snipers) {
            Square   sniper  = pop_lsb(snipers);
            Bitboard between = BETWEEN_BB[ksq][sniper] & occ;
            if (between && !more_than_one(between))
                pinned |= between & board.pieces(us);
        }
    }

    for (int i = 0; i < pseudo.count; ++i) {
        Move     m  = pseudo[i].move;
        PieceType pt = type_of(board.piece_on(m.from()));

        if (in_check
         || m.type() == EN_PASSANT
         || pt == KING
         || (pinned & square_bb(m.from()))) {
            if (board.is_legal(m)) return true;
        } else {
            return true;  // Definitely legal
        }
    }
    return false;
}

// ============================================================================
// Perft — correctness validation
// ============================================================================

uint64_t perft(Board& board, int depth) {
    if (depth == 0) return 1;

    MoveList legal;
    generate_legal(board, legal);

    if (depth == 1) return legal.count;

    uint64_t nodes = 0;
    StateInfo state;
    for (int i = 0; i < legal.count; ++i) {
        board.do_move(legal[i].move, state);
        nodes += perft(board, depth - 1);
        board.undo_move(legal[i].move);
    }
    return nodes;
}

} // namespace Chess
