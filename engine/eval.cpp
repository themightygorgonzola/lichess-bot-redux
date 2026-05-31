#include "eval.h"
#include "eval_params.h"
#include "bitboard.h"
#include "pawn_hash.h"

#include <cstdio>
#include <iostream>

namespace Chess
{

    // ============================================================================
    // Piece-Square Tables (PSTs)
    // These give a bonus/penalty for each piece on each square.
    // Values from the perspective of WHITE; flip for BLACK.
    // Tuned from various open-source engine resources.
    // ============================================================================

    namespace
    {

        // PIECE_VALUES is defined in eval.h (inline constexpr) and shared with search.
        // The local alias here keeps all the anonymous-namespace PST code unchanged.
        using Chess::PIECE_VALUES;

        // ============================================================================
        // PSTs now live in g_eval_params (eval_params.h).  The pointer tables below
        // point into the global struct so all existing PST_MG[pt][sq] / PST_EG[pt][sq]
        // call-sites continue to compile unchanged.
        // ============================================================================

        // PST array pointers indexed by PieceType — point into g_eval_params
        const int *PST_MG[PIECE_TYPE_NB] = {
            nullptr,
            g_eval_params.pst_pawn_mg,
            g_eval_params.pst_knight_mg,
            g_eval_params.pst_bishop_mg,
            g_eval_params.pst_rook_mg,
            g_eval_params.pst_queen_mg,
            g_eval_params.pst_king_mg};
        const int *PST_EG[PIECE_TYPE_NB] = {
            nullptr,
            g_eval_params.pst_pawn_eg,
            g_eval_params.pst_knight_eg,
            g_eval_params.pst_bishop_eg,
            g_eval_params.pst_rook_eg,
            g_eval_params.pst_queen_eg,
            g_eval_params.pst_king_eg};

        // Mirror a square for black (flip rank)
        constexpr Square mirror(Square s)
        {
            return Square(s ^ 56);
        }

        // Adjacent file masks — now in bitboard.h as ADJACENT_FILES_BB.
        // Local alias for minimal diff in the rest of this file.
        constexpr auto &ADJACENT_FILES = ADJACENT_FILES_BB;

        // passed_pawn_mask() — now in bitboard.h (shared with search.cpp)

        // Pawn structure evaluation terms are in g_eval_params (eval_params.h)

        // Game phase: 0 = endgame, 256 = opening/middlegame
        int game_phase(const Board &board)
        {
            int phase = 0;
            phase += popcount(board.pieces(KNIGHT)) * 1;
            phase += popcount(board.pieces(BISHOP)) * 1;
            phase += popcount(board.pieces(ROOK)) * 2;
            phase += popcount(board.pieces(QUEEN)) * 4;
            // Max phase = 4*1 + 4*1 + 4*2 + 2*4 = 24, scale to 256
            return std::min(phase * 256 / 24, 256);
        }

        // Endgame draw scaling — returns scale factor out of 256.
        // 256 = no change, 128 = halved, 0 = pure draw.
        int compute_scale_factor(const Board &board, int score, int phase)
        {
            int sf = 256;

            Bitboard wb = board.pieces(WHITE, BISHOP);
            Bitboard bb = board.pieces(BLACK, BISHOP);

            // --- Opposite-color bishops ---
            if (popcount(wb) == 1 && popcount(bb) == 1)
            {
                // Check if bishops are on opposite color squares
                bool w_on_light = (square_bb(lsb(wb)) & LIGHT_SQUARES) != 0;
                bool b_on_light = (square_bb(lsb(bb)) & LIGHT_SQUARES) != 0;
                if (w_on_light != b_on_light)
                {
                    int w_pawns = popcount(board.pieces(WHITE, PAWN));
                    int b_pawns = popcount(board.pieces(BLACK, PAWN));
                    if (w_pawns == 0 && b_pawns == 0)
                        sf = sf * g_eval_params.ocb_no_pawns_scale / 256;
                    else
                        sf = sf * g_eval_params.ocb_scale / 256;
                }
            }

            // --- KNNvK: two knights vs bare king is a draw ---
            {
                int total_pieces = popcount(board.pieces(WHITE) | board.pieces(BLACK));
                if (total_pieces == 4)
                {
                    Bitboard wn = board.pieces(WHITE, KNIGHT);
                    Bitboard bn = board.pieces(BLACK, KNIGHT);
                    // White has KNN, black has bare K
                    if (popcount(wn) == 2 && board.pieces(WHITE) == (wn | board.pieces(WHITE, KING)))
                        sf = 0;
                    // Black has KNN, white has bare K
                    if (popcount(bn) == 2 && board.pieces(BLACK) == (bn | board.pieces(BLACK, KING)))
                        sf = 0;
                }
            }

            // --- Pawn scarcity for winning side (EG only) ---
            if (sf > 0 && phase < 128 && std::abs(score) > 100)
            {
                Color winning = score > 0 ? WHITE : BLACK;
                int winning_pawns = popcount(board.pieces(winning, PAWN));
                int pawn_sf = std::min(256,
                                       g_eval_params.pawn_scarcity_base + winning_pawns * g_eval_params.pawn_scarcity_per_pawn);
                sf = sf * pawn_sf / 256;
            }

            return std::max(0, std::min(256, sf));
        }

    } // anonymous namespace

    void eval_init()
    {
        // Nothing to init for now — PSTs are constexpr
    }

    int evaluate_material(const Board &board)
    {
        int score = 0;
        for (PieceType pt = PAWN; pt <= QUEEN; pt = PieceType(int(pt) + 1))
        {
            score += PIECE_VALUES[pt] * popcount(board.pieces(WHITE, pt));
            score -= PIECE_VALUES[pt] * popcount(board.pieces(BLACK, pt));
        }
        return board.side_to_move() == WHITE ? score : -score;
    }

    // King safety table: indexed by total attack weight, hand-tuned curve.
    static constexpr int SAFETY_TABLE[100] = {
        0, 0, 1, 2, 3, 5, 7, 9, 12, 15,
        18, 22, 26, 30, 35, 40, 46, 52, 58, 65,
        72, 80, 88, 96, 105, 115, 125, 135, 146, 157,
        169, 181, 194, 207, 220, 234, 248, 263, 278, 294,
        310, 327, 344, 362, 380, 399, 418, 438, 458, 479,
        500, 522, 544, 567, 590, 614, 638, 663, 688, 714,
        740, 767, 794, 822, 850, 879, 908, 938, 968, 999,
        999, 999, 999, 999, 999, 999, 999, 999, 999, 999,
        999, 999, 999, 999, 999, 999, 999, 999, 999, 999,
        999, 999, 999, 999, 999, 999, 999, 999, 999, 999};

    int evaluate(const Board &board)
    {
        int mg_score = 0; // Middlegame score
        int eg_score = 0; // Endgame score

        // Material + Piece-Square Tables
        // WHITE: PST[mirror(s)] because PSTs are written rank8-first (visual layout)
        // BLACK: PST[s] directly (same visual convention already matches black's orientation)
        for (PieceType pt = PAWN; pt <= QUEEN; pt = PieceType(int(pt) + 1))
        {
            Bitboard white_pieces = board.pieces(WHITE, pt);
            Bitboard black_pieces = board.pieces(BLACK, pt);

            while (white_pieces)
            {
                Square s = pop_lsb(white_pieces);
                mg_score += PIECE_VALUES[pt] + PST_MG[pt][mirror(s)];
                eg_score += PIECE_VALUES[pt] + PST_EG[pt][mirror(s)];
            }

            while (black_pieces)
            {
                Square s = pop_lsb(black_pieces);
                mg_score -= PIECE_VALUES[pt] + PST_MG[pt][s];
                eg_score -= PIECE_VALUES[pt] + PST_EG[pt][s];
            }
        }

        // King evaluation (separate MG/EG tables)
        {
            Square wk = board.king_square(WHITE);
            Square bk = board.king_square(BLACK);
            mg_score += PST_MG[KING][mirror(wk)] - PST_MG[KING][bk];
            eg_score += PST_EG[KING][mirror(wk)] - PST_EG[KING][bk];
        }

        // Bishop pair bonus
        if (popcount(board.pieces(WHITE, BISHOP)) >= 2)
        {
            mg_score += g_eval_params.bishop_pair_bonus;
            eg_score += g_eval_params.bishop_pair_eg_bonus;
        }
        if (popcount(board.pieces(BLACK, BISHOP)) >= 2)
        {
            mg_score -= g_eval_params.bishop_pair_bonus;
            eg_score -= g_eval_params.bishop_pair_eg_bonus;
        }

        // Rook on open/semi-open file bonus
        for (File f = FILE_A; f < FILE_NB; ++f)
        {
            Bitboard file_mask = FILE_BB[f];
            bool white_pawns = board.pieces(WHITE, PAWN) & file_mask;
            bool black_pawns = board.pieces(BLACK, PAWN) & file_mask;

            if (board.pieces(WHITE, ROOK) & file_mask)
            {
                if (!white_pawns && !black_pawns)
                {
                    mg_score += g_eval_params.rook_open_file_bonus;
                    eg_score += g_eval_params.rook_open_file_eg;
                }
                else if (!white_pawns)
                {
                    mg_score += g_eval_params.rook_semi_open_bonus;
                    eg_score += g_eval_params.rook_semi_open_eg;
                }
            }
            if (board.pieces(BLACK, ROOK) & file_mask)
            {
                if (!white_pawns && !black_pawns)
                {
                    mg_score -= g_eval_params.rook_open_file_bonus;
                    eg_score -= g_eval_params.rook_open_file_eg;
                }
                else if (!black_pawns)
                {
                    mg_score -= g_eval_params.rook_semi_open_bonus;
                    eg_score -= g_eval_params.rook_semi_open_eg;
                }
            }
        }

        // Cached passer bitboards (b64+): populated by pawn-structure block,
        // consumed by passer-piece-interaction block below.
        Bitboard wp_passers = 0, bp_passers = 0;

        // Pawn structure (with pawn hash table caching)
        {
            Bitboard wp = board.pieces(WHITE, PAWN);
            Bitboard bp = board.pieces(BLACK, PAWN);

            // Use the board's dedicated pawn key
            uint64_t pawn_key = board.pawn_key();

            int pawn_mg = 0, pawn_eg = 0;
            if (!PawnTT.probe(pawn_key, pawn_mg, pawn_eg, wp_passers, bp_passers))
            {
                // Compute pawn structure from scratch
                // White pawns
                Bitboard white_temp = wp;
                while (white_temp)
                {
                    Square s = pop_lsb(white_temp);
                    File f = file_of(s);
                    Rank r = rank_of(s);

                    if (popcount(wp & FILE_BB[f]) > 1)
                        pawn_mg += g_eval_params.doubled_pawn_penalty;

                    if (!(wp & ADJACENT_FILES[f]))
                        pawn_mg += g_eval_params.isolated_pawn_penalty;

                    // Backward pawn: no friendly support behind, stop square attacked
                    if (r < RANK_7)
                    {
                        Bitboard adj = ADJACENT_FILES[f];
                        Bitboard at_or_behind = 0;
                        for (Rank rr = RANK_1; rr <= r; rr = Rank(int(rr) + 1))
                            at_or_behind |= RANK_BB[rr];
                        if (!(wp & adj & at_or_behind))
                        {
                            Square stop = Square(int(s) + 8);
                            if (pawn_attacks(WHITE, stop) & bp)
                            {
                                pawn_mg += g_eval_params.backward_pawn_penalty_mg;
                                pawn_eg += g_eval_params.backward_pawn_penalty_eg;
                            }
                        }
                    }

                    // Connected/defended pawn: supported by friendly pawn
                    bool is_connected_w = (pawn_attacks(BLACK, s) & wp) != 0;
                    if (is_connected_w)
                    {
                        pawn_mg += g_eval_params.connected_pawn_bonus_mg;
                        pawn_eg += g_eval_params.connected_pawn_bonus_eg;
                    }

                    if (!(bp & passed_pawn_mask(WHITE, s)))
                    {
                        wp_passers |= square_bb(s);
                        pawn_mg += g_eval_params.passed_pawn_bonus[r];
                        pawn_eg += g_eval_params.passed_pawn_eg_bonus[r];
                        // Protected passer: only if NOT already counted as connected
                        // (both conditions use the same pawn-support test — avoid double-count)
                        if (!is_connected_w && (pawn_attacks(BLACK, s) & wp))
                        {
                            pawn_mg += g_eval_params.protected_passer_mg;
                            pawn_eg += g_eval_params.protected_passer_eg;
                        }
                    }
                    else
                    {
                        // Candidate passer: only one enemy pawn blocks, our file has support
                        Bitboard blockers = bp & passed_pawn_mask(WHITE, s);
                        if (popcount(blockers) == 1)
                        {
                            pawn_mg += g_eval_params.candidate_passer_mg;
                            pawn_eg += g_eval_params.candidate_passer_eg;
                        }
                    }

                    // Center pawn advance bonus: d/e pawns on rank 4-5
                    if ((f == FILE_D || f == FILE_E) && r >= RANK_4 && r <= RANK_5)
                        pawn_mg += g_eval_params.center_pawn_advance_mg;
                }

                // Black pawns
                Bitboard black_temp = bp;
                while (black_temp)
                {
                    Square s = pop_lsb(black_temp);
                    File f = file_of(s);
                    Rank r = Rank(RANK_8 - rank_of(s));

                    if (popcount(bp & FILE_BB[f]) > 1)
                        pawn_mg -= g_eval_params.doubled_pawn_penalty;

                    if (!(bp & ADJACENT_FILES[f]))
                        pawn_mg -= g_eval_params.isolated_pawn_penalty;

                    // Backward pawn (for black)
                    {
                        Rank abs_r = rank_of(s);
                        if (abs_r > RANK_2)
                        {
                            Bitboard adj = ADJACENT_FILES[f];
                            Bitboard at_or_behind = 0;
                            for (Rank rr = abs_r; rr < RANK_NB; rr = Rank(int(rr) + 1))
                                at_or_behind |= RANK_BB[rr];
                            if (!(bp & adj & at_or_behind))
                            {
                                Square stop = Square(int(s) - 8);
                                if (pawn_attacks(BLACK, stop) & wp)
                                {
                                    pawn_mg -= g_eval_params.backward_pawn_penalty_mg;
                                    pawn_eg -= g_eval_params.backward_pawn_penalty_eg;
                                }
                            }
                        }
                    }

                    // Connected/defended pawn (for black)
                    bool is_connected_b = (pawn_attacks(WHITE, s) & bp) != 0;
                    if (is_connected_b)
                    {
                        pawn_mg -= g_eval_params.connected_pawn_bonus_mg;
                        pawn_eg -= g_eval_params.connected_pawn_bonus_eg;
                    }

                    if (!(wp & passed_pawn_mask(BLACK, s)))
                    {
                        bp_passers |= square_bb(s);
                        pawn_mg -= g_eval_params.passed_pawn_bonus[r];
                        pawn_eg -= g_eval_params.passed_pawn_eg_bonus[r];
                        // Protected passer: only if NOT already counted as connected
                        if (!is_connected_b && (pawn_attacks(WHITE, s) & bp))
                        {
                            pawn_mg -= g_eval_params.protected_passer_mg;
                            pawn_eg -= g_eval_params.protected_passer_eg;
                        }
                    }
                    else
                    {
                        // Candidate passer: only one enemy pawn blocks
                        Bitboard blockers = wp & passed_pawn_mask(BLACK, s);
                        if (popcount(blockers) == 1)
                        {
                            pawn_mg -= g_eval_params.candidate_passer_mg;
                            pawn_eg -= g_eval_params.candidate_passer_eg;
                        }
                    }

                    // Center pawn advance bonus (black): d/e pawns on abs rank 4-5
                    // r = RANK_8 - rank_of(s), so abs ranks 4-5 → r in [RANK_4, RANK_5]
                    if ((f == FILE_D || f == FILE_E) && r >= RANK_4 && r <= RANK_5)
                        pawn_mg -= g_eval_params.center_pawn_advance_mg;
                }

                PawnTT.store(pawn_key, pawn_mg, pawn_eg, wp_passers, bp_passers);
            }

            mg_score += pawn_mg;
            eg_score += pawn_eg;
        }

        // Precompute pawn attack maps (used by safe mobility, threats, space, weak minor)
        Bitboard white_pawn_attacks_bb = shift<NORTH_EAST>(board.pieces(WHITE, PAWN)) | shift<NORTH_WEST>(board.pieces(WHITE, PAWN));
        Bitboard black_pawn_attacks_bb = shift<SOUTH_EAST>(board.pieces(BLACK, PAWN)) | shift<SOUTH_WEST>(board.pieces(BLACK, PAWN));

        // Mobility bonus — MG + EG per piece, including queen
        // Knights & bishops use "safe" squares: not attacked by enemy pawns.
        // Rooks & queens use all non-own-piece squares (pawn attacks less relevant).
        {
            Bitboard occupied = board.pieces();
            Bitboard white_targets = ~board.pieces(WHITE);
            Bitboard black_targets = ~board.pieces(BLACK);

            // Safe squares for minor pieces: exclude enemy-pawn-attacked squares
            Bitboard white_safe = white_targets & ~black_pawn_attacks_bb;
            Bitboard black_safe = black_targets & ~white_pawn_attacks_bb;

            // Knights — safe mobility
            Bitboard knights = board.pieces(WHITE, KNIGHT);
            while (knights)
            {
                Square s = pop_lsb(knights);
                int moves = popcount(knight_attacks(s) & white_safe);
                mg_score += moves * g_eval_params.knight_mobility_mg;
                eg_score += moves * g_eval_params.knight_mobility_eg;
            }
            knights = board.pieces(BLACK, KNIGHT);
            while (knights)
            {
                Square s = pop_lsb(knights);
                int moves = popcount(knight_attacks(s) & black_safe);
                mg_score -= moves * g_eval_params.knight_mobility_mg;
                eg_score -= moves * g_eval_params.knight_mobility_eg;
            }

            // Bishops — safe mobility
            Bitboard bishops = board.pieces(WHITE, BISHOP);
            while (bishops)
            {
                Square s = pop_lsb(bishops);
                int moves = popcount(bishop_attacks(s, occupied) & white_safe);
                mg_score += moves * g_eval_params.bishop_mobility_mg;
                eg_score += moves * g_eval_params.bishop_mobility_eg;
            }
            bishops = board.pieces(BLACK, BISHOP);
            while (bishops)
            {
                Square s = pop_lsb(bishops);
                int moves = popcount(bishop_attacks(s, occupied) & black_safe);
                mg_score -= moves * g_eval_params.bishop_mobility_mg;
                eg_score -= moves * g_eval_params.bishop_mobility_eg;
            }

            // Rooks
            Bitboard rooks = board.pieces(WHITE, ROOK);
            while (rooks)
            {
                Square s = pop_lsb(rooks);
                int moves = popcount(rook_attacks(s, occupied) & white_targets);
                mg_score += moves * g_eval_params.rook_mobility_mg;
                eg_score += moves * g_eval_params.rook_mobility_eg;
            }
            rooks = board.pieces(BLACK, ROOK);
            while (rooks)
            {
                Square s = pop_lsb(rooks);
                int moves = popcount(rook_attacks(s, occupied) & black_targets);
                mg_score -= moves * g_eval_params.rook_mobility_mg;
                eg_score -= moves * g_eval_params.rook_mobility_eg;
            }

            // Queens
            Bitboard queens = board.pieces(WHITE, QUEEN);
            while (queens)
            {
                Square s = pop_lsb(queens);
                int moves = popcount(queen_attacks(s, occupied) & white_targets);
                mg_score += moves * g_eval_params.queen_mobility_mg;
                eg_score += moves * g_eval_params.queen_mobility_eg;
            }
            queens = board.pieces(BLACK, QUEEN);
            while (queens)
            {
                Square s = pop_lsb(queens);
                int moves = popcount(queen_attacks(s, occupied) & black_targets);
                mg_score -= moves * g_eval_params.queen_mobility_mg;
                eg_score -= moves * g_eval_params.queen_mobility_eg;
            }
        }

        // Rook on 7th rank bonus + connected rooks
        {
            Bitboard occupied = board.pieces();

            // White rooks
            Bitboard wr = board.pieces(WHITE, ROOK);
            Bitboard wr_copy = wr;
            while (wr_copy)
            {
                Square s = pop_lsb(wr_copy);
                // Rook on 7th rank (relative): strong if enemy king on 8th or enemy pawns on 7th
                if (rank_of(s) == RANK_7 &&
                    (rank_of(board.king_square(BLACK)) == RANK_8 || (board.pieces(BLACK, PAWN) & RANK_7_BB)))
                {
                    mg_score += g_eval_params.rook_seventh_mg;
                    eg_score += g_eval_params.rook_seventh_eg;
                }
            }
            // Connected rooks: two rooks that see each other (same rank or file, no pieces between)
            if (popcount(wr) >= 2)
            {
                Bitboard rooks_temp = wr;
                Square r1 = pop_lsb(rooks_temp);
                Square r2 = pop_lsb(rooks_temp);
                if (rook_attacks(r1, occupied) & square_bb(r2))
                {
                    mg_score += g_eval_params.connected_rooks_bonus;
                }
            }

            // Black rooks
            Bitboard br = board.pieces(BLACK, ROOK);
            Bitboard br_copy = br;
            while (br_copy)
            {
                Square s = pop_lsb(br_copy);
                if (rank_of(s) == RANK_2 &&
                    (rank_of(board.king_square(WHITE)) == RANK_1 || (board.pieces(WHITE, PAWN) & RANK_2_BB)))
                {
                    mg_score -= g_eval_params.rook_seventh_mg;
                    eg_score -= g_eval_params.rook_seventh_eg;
                }
            }
            if (popcount(br) >= 2)
            {
                Bitboard rooks_temp = br;
                Square r1 = pop_lsb(rooks_temp);
                Square r2 = pop_lsb(rooks_temp);
                if (rook_attacks(r1, occupied) & square_bb(r2))
                {
                    mg_score -= g_eval_params.connected_rooks_bonus;
                }
            }
        }

        // Outpost squares: bonus for knights/bishops on squares not attackable by enemy pawns
        // and supported by a friendly pawn
        {
            Bitboard wp = board.pieces(WHITE, PAWN);
            Bitboard bp = board.pieces(BLACK, PAWN);

            // White outposts on ranks 4-6
            Bitboard white_outpost_ranks = RANK_4_BB | RANK_5_BB | RANK_6_BB;
            Bitboard wn = board.pieces(WHITE, KNIGHT);
            while (wn)
            {
                Square s = pop_lsb(wn);
                if (!(square_bb(s) & white_outpost_ranks))
                    continue;
                File f = file_of(s);
                // No enemy pawns on adjacent files that could attack this square
                Bitboard enemy_pawn_threat = ADJACENT_FILES[f];
                // Only pawns BEHIND this rank (from black's perspective = above)
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
                    enemy_front |= RANK_BB[rr];
                if (!(bp & enemy_pawn_threat & enemy_front))
                {
                    // Supported by friendly pawn?
                    if (pawn_attacks(BLACK, s) & wp)
                    { // BLACK attack pattern = squares that white pawns attack from
                        mg_score += g_eval_params.knight_outpost_supported_mg;
                        eg_score += g_eval_params.knight_outpost_supported_eg;
                    }
                    else
                    {
                        mg_score += g_eval_params.knight_outpost_mg;
                        eg_score += g_eval_params.knight_outpost_eg;
                    }
                }
            }

            Bitboard wb = board.pieces(WHITE, BISHOP);
            while (wb)
            {
                Square s = pop_lsb(wb);
                if (!(square_bb(s) & white_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_pawn_threat = ADJACENT_FILES[f];
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
                    enemy_front |= RANK_BB[rr];
                if (!(bp & enemy_pawn_threat & enemy_front))
                {
                    if (pawn_attacks(BLACK, s) & wp)
                    {
                        mg_score += g_eval_params.bishop_outpost_supported_mg;
                        eg_score += g_eval_params.bishop_outpost_supported_eg;
                    }
                    else
                    {
                        mg_score += g_eval_params.bishop_outpost_mg;
                    }
                }
            }

            // Black outposts on ranks 5-3 (from black's perspective = ranks 3-5 absolute)
            Bitboard black_outpost_ranks = RANK_3_BB | RANK_4_BB | RANK_5_BB;
            Bitboard bn = board.pieces(BLACK, KNIGHT);
            while (bn)
            {
                Square s = pop_lsb(bn);
                if (!(square_bb(s) & black_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_pawn_threat = ADJACENT_FILES[f];
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) - 1); rr >= RANK_1; rr = Rank(int(rr) - 1))
                    enemy_front |= RANK_BB[rr];
                if (!(wp & enemy_pawn_threat & enemy_front))
                {
                    if (pawn_attacks(WHITE, s) & bp)
                    {
                        mg_score -= g_eval_params.knight_outpost_supported_mg;
                        eg_score -= g_eval_params.knight_outpost_supported_eg;
                    }
                    else
                    {
                        mg_score -= g_eval_params.knight_outpost_mg;
                        eg_score -= g_eval_params.knight_outpost_eg;
                    }
                }
            }

            Bitboard bb = board.pieces(BLACK, BISHOP);
            while (bb)
            {
                Square s = pop_lsb(bb);
                if (!(square_bb(s) & black_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_pawn_threat = ADJACENT_FILES[f];
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) - 1); rr >= RANK_1; rr = Rank(int(rr) - 1))
                    enemy_front |= RANK_BB[rr];
                if (!(wp & enemy_pawn_threat & enemy_front))
                {
                    if (pawn_attacks(WHITE, s) & bp)
                    {
                        mg_score -= g_eval_params.bishop_outpost_supported_mg;
                        eg_score -= g_eval_params.bishop_outpost_supported_eg;
                    }
                    else
                    {
                        mg_score -= g_eval_params.bishop_outpost_mg;
                    }
                }
            }
        }

        // ========================================================================
        // Extended evaluation terms
        // ========================================================================

        // (pawn attack maps already computed above for safe mobility)

        // --- Pinned piece penalty ---
        // A piece is pinned if it's the only blocker between an enemy slider and our king
        {
            Bitboard occupied = board.pieces();
            for (Color c : {WHITE, BLACK})
            {
                Color them = ~c;
                Square ksq = board.king_square(c);
                Bitboard our = board.pieces(c);

                // Snipers: enemy sliders that could attack king on an empty board
                Bitboard rq = board.pieces(them, ROOK, QUEEN);
                Bitboard bq = board.pieces(them, BISHOP, QUEEN);
                Bitboard snipers = (rook_attacks(ksq, 0) & rq) | (bishop_attacks(ksq, 0) & bq);

                Bitboard pinned = 0;
                while (snipers)
                {
                    Square s = pop_lsb(snipers);
                    Bitboard between = BETWEEN_BB[ksq][s] & occupied;
                    if (!more_than_one(between))
                    {
                        pinned |= between & our; // exactly one of our pieces = pinned
                    }
                }

                int pin_count = popcount(pinned);
                if (c == WHITE)
                {
                    mg_score += pin_count * g_eval_params.pinned_piece_penalty_mg;
                    eg_score += pin_count * g_eval_params.pinned_piece_penalty_eg;
                }
                else
                {
                    mg_score -= pin_count * g_eval_params.pinned_piece_penalty_mg;
                    eg_score -= pin_count * g_eval_params.pinned_piece_penalty_eg;
                }
            }
        }

        // --- Pin-creation bonus: reward our sliders that X-ray through an enemy
        //     piece to a more valuable enemy piece (or enemy king).
        //     Example: Our Bg5 attacks enemy Nf6 which shields the enemy queen d8.
        {
            // Piece value table for comparison (indexed by PieceType)
            static constexpr int PV[] = {0, 100, 320, 330, 500, 900, 20000};

            Bitboard occupied = board.pieces();
            for (Color c : {WHITE, BLACK})
            {
                Color them = ~c;
                int pin_bonus = 0;

                // Our bishops / queens (diagonal snipers)
                Bitboard our_diag = board.pieces(c, BISHOP) | board.pieces(c, QUEEN);
                while (our_diag)
                {
                    Square s = pop_lsb(our_diag);
                    Bitboard atts = bishop_attacks(s, occupied);
                    // For each enemy piece we attack on a diagonal...
                    Bitboard victims = atts & board.pieces(them);
                    while (victims)
                    {
                        Square v = pop_lsb(victims);
                        // What's behind this piece on the same ray?
                        // Remove the victim from occupied, re-shoot the ray
                        Bitboard xray = bishop_attacks(s, occupied ^ square_bb(v));
                        // Pin is meaningful only if pinned to queen or king
                        Bitboard behind = (xray & ~atts) & board.pieces(them);
                        while (behind)
                        {
                            Square b = pop_lsb(behind);
                            PieceType vpt = type_of(board.piece_on(v));
                            PieceType bpt = type_of(board.piece_on(b));
                            if ((bpt == QUEEN || bpt == KING) && PV[bpt] > PV[vpt])
                            {
                                pin_bonus++;
                            }
                        }
                    }
                }

                // Our rooks / queens (straight snipers)
                Bitboard our_orth = board.pieces(c, ROOK) | board.pieces(c, QUEEN);
                while (our_orth)
                {
                    Square s = pop_lsb(our_orth);
                    Bitboard atts = rook_attacks(s, occupied);
                    Bitboard victims = atts & board.pieces(them);
                    while (victims)
                    {
                        Square v = pop_lsb(victims);
                        Bitboard xray = rook_attacks(s, occupied ^ square_bb(v));
                        Bitboard behind = (xray & ~atts) & board.pieces(them);
                        while (behind)
                        {
                            Square b = pop_lsb(behind);
                            PieceType vpt = type_of(board.piece_on(v));
                            PieceType bpt = type_of(board.piece_on(b));
                            if ((bpt == QUEEN || bpt == KING) && PV[bpt] > PV[vpt])
                            {
                                pin_bonus++;
                            }
                        }
                    }
                }

                if (c == WHITE)
                {
                    mg_score += pin_bonus * g_eval_params.pin_creation_bonus_mg;
                    eg_score += pin_bonus * g_eval_params.pin_creation_bonus_eg;
                }
                else
                {
                    mg_score -= pin_bonus * g_eval_params.pin_creation_bonus_mg;
                    eg_score -= pin_bonus * g_eval_params.pin_creation_bonus_eg;
                }
            }
        }

        // --- Bad bishop: penalty for own pawns on same color complex ---
        {
            Bitboard wb = board.pieces(WHITE, BISHOP);
            while (wb)
            {
                Square s = pop_lsb(wb);
                Bitboard color_complex = (square_bb(s) & DARK_SQUARES) ? DARK_SQUARES : LIGHT_SQUARES;
                int own_pawns_on_color = popcount(board.pieces(WHITE, PAWN) & color_complex);
                mg_score += own_pawns_on_color * g_eval_params.bad_bishop_per_pawn_mg;
                eg_score += own_pawns_on_color * g_eval_params.bad_bishop_per_pawn_eg;
            }
            Bitboard bb_b = board.pieces(BLACK, BISHOP);
            while (bb_b)
            {
                Square s = pop_lsb(bb_b);
                Bitboard color_complex = (square_bb(s) & DARK_SQUARES) ? DARK_SQUARES : LIGHT_SQUARES;
                int own_pawns_on_color = popcount(board.pieces(BLACK, PAWN) & color_complex);
                mg_score -= own_pawns_on_color * g_eval_params.bad_bishop_per_pawn_mg;
                eg_score -= own_pawns_on_color * g_eval_params.bad_bishop_per_pawn_eg;
            }
        }

        // --- Weak color complex (EG): one side has bishop(s), opponent has none.
        // The bishop-owner dominates all squares of their bishop's color unopposed.
        // Audit b88: weak_color_complex SF=534, d1=397 (Δ137cp).
        {
            Bitboard wb = board.pieces(WHITE, BISHOP);
            Bitboard bb2 = board.pieces(BLACK, BISHOP);
            int w_bish = popcount(wb);
            int b_bish = popcount(bb2);
            if (w_bish >= 1 && b_bish == 0)
            {
                Bitboard wb_temp = wb;
                while (wb_temp)
                {
                    Square bsq = pop_lsb(wb_temp);
                    Bitboard dominated = (square_bb(bsq) & DARK_SQUARES) ? DARK_SQUARES : LIGHT_SQUARES;
                    int ep = popcount(board.pieces(BLACK, PAWN) & dominated);
                    eg_score += g_eval_params.color_complex_bishop_eg + ep * g_eval_params.color_complex_pawn_eg;
                }
            }
            if (b_bish >= 1 && w_bish == 0)
            {
                Bitboard bb_temp = bb2;
                while (bb_temp)
                {
                    Square bsq = pop_lsb(bb_temp);
                    Bitboard dominated = (square_bb(bsq) & DARK_SQUARES) ? DARK_SQUARES : LIGHT_SQUARES;
                    int ep = popcount(board.pieces(WHITE, PAWN) & dominated);
                    eg_score -= g_eval_params.color_complex_bishop_eg + ep * g_eval_params.color_complex_pawn_eg;
                }
            }
        }

        // --- Threats: low-value piece attacking higher-value piece ---
        {
            Bitboard occupied = board.pieces();

            // White threats on black pieces
            Bitboard black_non_pawns = board.pieces(BLACK, KNIGHT) | board.pieces(BLACK, BISHOP) | board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN);
            int w_pawn_threats = popcount(white_pawn_attacks_bb & black_non_pawns);
            mg_score += w_pawn_threats * g_eval_params.threat_by_pawn_mg;
            eg_score += w_pawn_threats * g_eval_params.threat_by_pawn_eg;

            // White minor attacks on rooks/queens
            Bitboard black_majors = board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN);
            Bitboard w_minor_att = 0;
            Bitboard wn = board.pieces(WHITE, KNIGHT);
            while (wn)
            {
                w_minor_att |= knight_attacks(pop_lsb(wn));
            }
            Bitboard wbishops = board.pieces(WHITE, BISHOP);
            while (wbishops)
            {
                w_minor_att |= bishop_attacks(pop_lsb(wbishops), occupied);
            }
            int w_minor_threats = popcount(w_minor_att & black_majors);
            mg_score += w_minor_threats * g_eval_params.threat_by_minor_mg;
            eg_score += w_minor_threats * g_eval_params.threat_by_minor_eg;

            // White rook attacks on queen
            Bitboard w_rook_att = 0;
            Bitboard wr = board.pieces(WHITE, ROOK);
            while (wr)
            {
                w_rook_att |= rook_attacks(pop_lsb(wr), occupied);
            }
            int w_rook_threats = popcount(w_rook_att & board.pieces(BLACK, QUEEN));
            mg_score += w_rook_threats * g_eval_params.threat_by_rook_mg;
            eg_score += w_rook_threats * g_eval_params.threat_by_rook_eg;

            // Black threats on white pieces (mirror)
            Bitboard white_non_pawns = board.pieces(WHITE, KNIGHT) | board.pieces(WHITE, BISHOP) | board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN);
            int b_pawn_threats = popcount(black_pawn_attacks_bb & white_non_pawns);
            mg_score -= b_pawn_threats * g_eval_params.threat_by_pawn_mg;
            eg_score -= b_pawn_threats * g_eval_params.threat_by_pawn_eg;

            Bitboard white_majors = board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN);
            Bitboard b_minor_att = 0;
            Bitboard bn = board.pieces(BLACK, KNIGHT);
            while (bn)
            {
                b_minor_att |= knight_attacks(pop_lsb(bn));
            }
            Bitboard bbishops = board.pieces(BLACK, BISHOP);
            while (bbishops)
            {
                b_minor_att |= bishop_attacks(pop_lsb(bbishops), occupied);
            }
            int b_minor_threats = popcount(b_minor_att & white_majors);
            mg_score -= b_minor_threats * g_eval_params.threat_by_minor_mg;
            eg_score -= b_minor_threats * g_eval_params.threat_by_minor_eg;

            Bitboard b_rook_att = 0;
            Bitboard br = board.pieces(BLACK, ROOK);
            while (br)
            {
                b_rook_att |= rook_attacks(pop_lsb(br), occupied);
            }
            int b_rook_threats = popcount(b_rook_att & board.pieces(WHITE, QUEEN));
            mg_score -= b_rook_threats * g_eval_params.threat_by_rook_mg;
            eg_score -= b_rook_threats * g_eval_params.threat_by_rook_eg;
        }

        // --- Space advantage: safe central squares not attacked by enemy pawns ---
        {
            constexpr Bitboard CENTER_FILES = FILE_C_BB | FILE_D_BB | FILE_E_BB | FILE_F_BB;
            constexpr Bitboard WHITE_SPACE = CENTER_FILES & (RANK_2_BB | RANK_3_BB | RANK_4_BB);
            constexpr Bitboard BLACK_SPACE = CENTER_FILES & (RANK_5_BB | RANK_6_BB | RANK_7_BB);

            mg_score += popcount(WHITE_SPACE & ~black_pawn_attacks_bb) * g_eval_params.space_bonus_mg;
            mg_score -= popcount(BLACK_SPACE & ~white_pawn_attacks_bb) * g_eval_params.space_bonus_mg;
        }

        // --- Rook behind passed pawn + King-passer distance (EG) ---
        {
            Square wk = board.king_square(WHITE);
            Square bk = board.king_square(BLACK);

            // White passed pawns (b64: iterate cached passer bitboard from pawn hash)
            Bitboard temp = wp_passers;
            while (temp)
            {
                Square s = pop_lsb(temp);
                {
                    File f = file_of(s);
                    // Rook behind this passer (same file, lower rank)
                    Bitboard behind = FILE_BB[f];
                    Bitboard behind_ranks = 0;
                    for (Rank rr = RANK_1; rr < rank_of(s); rr = Rank(int(rr) + 1))
                        behind_ranks |= RANK_BB[rr];
                    if (board.pieces(WHITE, ROOK) & behind & behind_ranks)
                    {
                        mg_score += g_eval_params.rook_behind_passer_mg;
                        eg_score += g_eval_params.rook_behind_passer_eg;
                    }
                    // Enemy rook on passer file: Black rook anywhere on the f-file
                    // can intercept or blockade the passer — discount the bonus.
                    if (board.pieces(BLACK, ROOK) & FILE_BB[f])
                    {
                        mg_score += g_eval_params.enemy_rook_on_passer_file_mg;
                        eg_score += g_eval_params.enemy_rook_on_passer_file_eg;
                    }
                    // King-passer distance (EG) — Chebyshev (king steps), not Manhattan
                    int wk_dist = std::max(std::abs(int(file_of(wk)) - int(f)),
                                           std::abs(int(rank_of(wk)) - int(rank_of(s))));
                    int bk_dist = std::max(std::abs(int(file_of(bk)) - int(f)),
                                           std::abs(int(rank_of(bk)) - int(rank_of(s))));
                    mg_score += (6 - wk_dist) * g_eval_params.king_passer_support_mg;
                    mg_score += bk_dist * g_eval_params.king_passer_threat_mg;
                    eg_score += (6 - wk_dist) * g_eval_params.king_passer_support_eg;
                    eg_score += bk_dist * g_eval_params.king_passer_threat_eg;

                    // Wrong-colour bishop draw: if White has only one bishop and it
                    // is the wrong colour for this pawn's promotion square, the
                    // endgame is likely drawn — apply a large EG penalty.
                    {
                        Bitboard wb = board.pieces(WHITE, BISHOP);
                        if (popcount(wb) == 1 &&
                            !(board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN) | board.pieces(WHITE, KNIGHT)))
                        {
                            bool bishop_on_dark = (wb & DARK_SQUARES) != 0;
                            Square promo = make_square(f, RANK_8);
                            bool promo_on_dark = (square_bb(promo) & DARK_SQUARES) != 0;
                            if (bishop_on_dark != promo_on_dark && (f == FILE_A || f == FILE_H))
                                eg_score += g_eval_params.wrong_bishop_passer_penalty_eg;
                        }
                    }

                    // Blockade: enemy non-pawn piece on the stop square.
                    // Cancel the full base passer bonus (already in pawn_eg) and add
                    // an extra piece-blockade penalty — the pawn is frozen indefinitely.
                    Square stop_sq = Square(int(s) + 8);
                    if (rank_of(s) < RANK_8 && (board.pieces(BLACK) & ~board.pieces(BLACK, PAWN) & square_bb(stop_sq)))
                    {
                        eg_score -= g_eval_params.passed_pawn_eg_bonus[rank_of(s)]; // b90: cancel pawn_eg contribution (was /2)
                        eg_score -= g_eval_params.passer_blockade_piece_eg;         // b90: extra penalty for piece blockade
                    }

                    // Outside passed pawn (EG): passer is on a wing with NO enemy pawns;
                    // enemy pawns are all on the opposite wing — huge K+P ending advantage.
                    // Audit b88: SF=1055, d1=135 (CRITICAL Δ920cp).
                    {
                        constexpr Bitboard QS = FILE_A_BB | FILE_B_BB | FILE_C_BB | FILE_D_BB;
                        constexpr Bitboard KS = FILE_E_BB | FILE_F_BB | FILE_G_BB | FILE_H_BB;
                        bool passer_qs = file_of(s) <= FILE_D;
                        Bitboard ep = board.pieces(BLACK, PAWN);
                        if (!(ep & (passer_qs ? QS : KS)) && (ep & (passer_qs ? KS : QS)))
                            eg_score += g_eval_params.outside_passer_eg;
                    }

                    // Unstoppable passer — Rule of the Square (EG):
                    // defending king is outside the square and can't catch the passer.
                    // Only check in simplified positions (no rooks/queens to intercept).
                    {
                        int n = int(RANK_8) - int(rank_of(s)); // moves to promote
                        if (n > 0 && !(board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN) |
                                       board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN)))
                        {
                            int king_dist = std::max(
                                std::abs(int(file_of(bk)) - int(file_of(s))),
                                int(RANK_8) - int(rank_of(bk)));
                            if (king_dist > n + 1)
                                eg_score += g_eval_params.unstoppable_passer_eg;
                        }
                    }
                }
            }

            // Black passed pawns (b64: iterate cached passer bitboard from pawn hash)
            temp = bp_passers;
            while (temp)
            {
                Square s = pop_lsb(temp);
                {
                    File f = file_of(s);
                    // Rook behind this passer (same file, higher rank)
                    Bitboard behind = FILE_BB[f];
                    Bitboard behind_ranks = 0;
                    for (Rank rr = Rank(rank_of(s) + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
                        behind_ranks |= RANK_BB[rr];
                    if (board.pieces(BLACK, ROOK) & behind & behind_ranks)
                    {
                        mg_score -= g_eval_params.rook_behind_passer_mg;
                        eg_score -= g_eval_params.rook_behind_passer_eg;
                    }
                    // Enemy rook on passer file: White rook anywhere on the f-file
                    // can intercept or blockade the passer — discount the bonus.
                    if (board.pieces(WHITE, ROOK) & FILE_BB[f])
                    {
                        mg_score -= g_eval_params.enemy_rook_on_passer_file_mg;
                        eg_score -= g_eval_params.enemy_rook_on_passer_file_eg;
                    }
                    // King-passer distance (EG) — Chebyshev (king steps), not Manhattan
                    int bk_dist = std::max(std::abs(int(file_of(bk)) - int(f)),
                                           std::abs(int(rank_of(bk)) - int(rank_of(s))));
                    int wk_dist = std::max(std::abs(int(file_of(wk)) - int(f)),
                                           std::abs(int(rank_of(wk)) - int(rank_of(s))));
                    mg_score -= (6 - bk_dist) * g_eval_params.king_passer_support_mg;
                    mg_score -= wk_dist * g_eval_params.king_passer_threat_mg;
                    eg_score -= (6 - bk_dist) * g_eval_params.king_passer_support_eg;
                    eg_score -= wk_dist * g_eval_params.king_passer_threat_eg;

                    // Wrong-colour bishop draw: if Black has only one bishop and it
                    // is the wrong colour for this pawn's promotion square.
                    {
                        Bitboard bb_b = board.pieces(BLACK, BISHOP);
                        if (popcount(bb_b) == 1 &&
                            !(board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN) | board.pieces(BLACK, KNIGHT)))
                        {
                            bool bishop_on_dark = (bb_b & DARK_SQUARES) != 0;
                            Square promo = make_square(f, RANK_1);
                            bool promo_on_dark = (square_bb(promo) & DARK_SQUARES) != 0;
                            if (bishop_on_dark != promo_on_dark && (f == FILE_A || f == FILE_H))
                                eg_score -= g_eval_params.wrong_bishop_passer_penalty_eg;
                        }
                    }

                    // Blockade: enemy non-pawn piece on the stop square.
                    // Cancel the full base passer bonus (already in pawn_eg) and add
                    // an extra piece-blockade penalty — the pawn is frozen indefinitely.
                    Square stop_sq = Square(int(s) - 8);
                    if (rank_of(s) > RANK_1 && (board.pieces(WHITE) & ~board.pieces(WHITE, PAWN) & square_bb(stop_sq)))
                    {
                        eg_score += g_eval_params.passed_pawn_eg_bonus[RANK_8 - rank_of(s)]; // b90: cancel pawn_eg contribution (was /2)
                        eg_score += g_eval_params.passer_blockade_piece_eg;                  // b90: extra penalty for piece blockade
                    }

                    // Outside passed pawn for Black
                    {
                        constexpr Bitboard QS = FILE_A_BB | FILE_B_BB | FILE_C_BB | FILE_D_BB;
                        constexpr Bitboard KS = FILE_E_BB | FILE_F_BB | FILE_G_BB | FILE_H_BB;
                        bool passer_qs = file_of(s) <= FILE_D;
                        Bitboard ep = board.pieces(WHITE, PAWN);
                        if (!(ep & (passer_qs ? QS : KS)) && (ep & (passer_qs ? KS : QS)))
                            eg_score -= g_eval_params.outside_passer_eg;
                    }

                    // Unstoppable passer for Black — Rule of the Square (EG)
                    {
                        int n = int(rank_of(s)); // moves to promote to rank 1 (RANK_1 == 0)
                        if (n > 0 && !(board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN) |
                                       board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN)))
                        {
                            int king_dist = std::max(
                                std::abs(int(file_of(wk)) - int(file_of(s))),
                                int(rank_of(wk)));
                            if (king_dist > n + 1)
                                eg_score -= g_eval_params.unstoppable_passer_eg;
                        }
                    }
                }
            }
        }

        // --- Weak / undefended minor pieces: minor not pawn-defended, attacked by enemy pawn ---
        {
            // White minors not defended by own pawns but attacked by enemy pawns
            Bitboard w_minors = board.pieces(WHITE, KNIGHT) | board.pieces(WHITE, BISHOP);
            Bitboard weak_w = (w_minors & ~white_pawn_attacks_bb) & black_pawn_attacks_bb;
            mg_score += popcount(weak_w) * g_eval_params.weak_minor_penalty_mg;
            eg_score += popcount(weak_w) * g_eval_params.weak_minor_penalty_eg;

            // Black minors
            Bitboard b_minors = board.pieces(BLACK, KNIGHT) | board.pieces(BLACK, BISHOP);
            Bitboard weak_b = (b_minors & ~black_pawn_attacks_bb) & white_pawn_attacks_bb;
            mg_score -= popcount(weak_b) * g_eval_params.weak_minor_penalty_mg;
            eg_score -= popcount(weak_b) * g_eval_params.weak_minor_penalty_eg;
        }

        // King Safety — middlegame-only evaluation
        // Counts attackers to the king zone and penalizes unsafe positions
        {
            Bitboard occupied = board.pieces();

            for (Color c : {WHITE, BLACK})
            {
                Color them = ~c;
                Square ksq = board.king_square(c);
                Bitboard king_zone = king_attacks(ksq) | square_bb(ksq);

                int attackers_count = 0;
                int attack_weight = 0;

                // Knight attackers to king zone
                Bitboard enemy_knights = board.pieces(them, KNIGHT);
                while (enemy_knights)
                {
                    Square s = pop_lsb(enemy_knights);
                    if (knight_attacks(s) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_knight;
                    }
                }

                // Bishop attackers to king zone
                Bitboard enemy_bishops = board.pieces(them, BISHOP);
                while (enemy_bishops)
                {
                    Square s = pop_lsb(enemy_bishops);
                    if (bishop_attacks(s, occupied) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_bishop;
                    }
                }

                // Rook attackers to king zone
                Bitboard enemy_rooks = board.pieces(them, ROOK);
                while (enemy_rooks)
                {
                    Square s = pop_lsb(enemy_rooks);
                    if (rook_attacks(s, occupied) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_rook;
                    }
                }

                // Queen attackers to king zone
                Bitboard enemy_queens = board.pieces(them, QUEEN);
                while (enemy_queens)
                {
                    Square s = pop_lsb(enemy_queens);
                    if (queen_attacks(s, occupied) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_queen;
                    }
                }

                int safety_penalty = 0;
                if (attack_weight > 0)
                {
                    safety_penalty = SAFETY_TABLE[std::min(attack_weight, 99)];
                }

                // Pawn shield bonus: friendly pawns in front of king
                Bitboard shield;
                File kf = file_of(ksq);
                Bitboard shield_files = FILE_BB[kf];
                if (kf > FILE_A)
                    shield_files |= FILE_BB[kf - 1];
                if (kf < FILE_H)
                    shield_files |= FILE_BB[kf + 1];

                if (c == WHITE)
                {
                    Rank kr = rank_of(ksq);
                    Bitboard shield_ranks = 0;
                    if (kr < RANK_8)
                        shield_ranks |= RANK_BB[kr + 1];
                    if (kr + 1 < RANK_8)
                        shield_ranks |= RANK_BB[kr + 2];
                    shield = board.pieces(WHITE, PAWN) & shield_files & shield_ranks;
                }
                else
                {
                    Rank kr = rank_of(ksq);
                    Bitboard shield_ranks = 0;
                    if (kr > RANK_1)
                        shield_ranks |= RANK_BB[kr - 1];
                    if (kr - 1 > RANK_1)
                        shield_ranks |= RANK_BB[kr - 2];
                    shield = board.pieces(BLACK, PAWN) & shield_files & shield_ranks;
                }
                int shield_bonus = popcount(shield) * g_eval_params.pawn_shield_bonus;

                // Open files near king: penalize if no friendly pawns on king's files
                int open_file_penalty = 0;
                for (File f = std::max(FILE_A, File(kf - 1)); f <= std::min(FILE_H, File(kf + 1)); ++f)
                {
                    if (!(board.pieces(c, PAWN) & FILE_BB[f]))
                    {
                        open_file_penalty += g_eval_params.king_open_file_penalty;
                        // Extra penalty if no enemy pawns either (fully open)
                        if (!(board.pieces(them, PAWN) & FILE_BB[f]))
                            open_file_penalty += g_eval_params.king_open_file_full_extra;
                    }
                }

                int king_score = -safety_penalty + shield_bonus - open_file_penalty;
                if (c == WHITE)
                    mg_score += king_score;
                else
                    mg_score -= king_score;
            }
        }

        // Castling evaluation: urgency penalty + castled bonus
        {
            Square wk = board.king_square(WHITE);
            Square bk = board.king_square(BLACK);
            CastlingRight cr = board.castling_rights();
            bool w_castle_right = (cr & (WHITE_OO | WHITE_OOO)) != NO_CASTLING;
            bool b_castle_right = (cr & (BLACK_OO | BLACK_OOO)) != NO_CASTLING;
            // Slight urgency to castle while still on e1/e8 with rights remaining
            if (wk == E1 && w_castle_right)
                mg_score -= g_eval_params.castling_urgency_penalty;
            if (bk == E8 && b_castle_right)
                mg_score += g_eval_params.castling_urgency_penalty;

            // Bonus for having castled (king on typical castled squares)
            if (wk == G1 || wk == C1)
                mg_score += g_eval_params.castled_bonus_mg;
            if (bk == G8 || bk == C8)
                mg_score -= g_eval_params.castled_bonus_mg;
        }

        // Queen early development penalty: penalise queen leaving its home square
        // while own minor pieces (N on b1/g1, B on c1/f1) are still undeveloped.
        // Discourages premature Qb3/Qc2 sorties before pieces are developed.
        if (g_eval_params.queen_early_dev_penalty_mg != 0)
        {
            // White
            Bitboard wq = board.pieces(WHITE, QUEEN);
            if (wq && !(wq & square_bb(D1)))
            {
                int undeveloped = 0;
                if (board.pieces(WHITE, KNIGHT) & square_bb(B1))
                    undeveloped++;
                if (board.pieces(WHITE, KNIGHT) & square_bb(G1))
                    undeveloped++;
                if (board.pieces(WHITE, BISHOP) & square_bb(C1))
                    undeveloped++;
                if (board.pieces(WHITE, BISHOP) & square_bb(F1))
                    undeveloped++;
                mg_score -= undeveloped * g_eval_params.queen_early_dev_penalty_mg;
            }
            // Black
            Bitboard bq = board.pieces(BLACK, QUEEN);
            if (bq && !(bq & square_bb(D8)))
            {
                int undeveloped = 0;
                if (board.pieces(BLACK, KNIGHT) & square_bb(B8))
                    undeveloped++;
                if (board.pieces(BLACK, KNIGHT) & square_bb(G8))
                    undeveloped++;
                if (board.pieces(BLACK, BISHOP) & square_bb(C8))
                    undeveloped++;
                if (board.pieces(BLACK, BISHOP) & square_bb(F8))
                    undeveloped++;
                mg_score += undeveloped * g_eval_params.queen_early_dev_penalty_mg;
            }
        }

        // Mopup evaluation: in winning endgames, push enemy king to corner
        {
            int mat_diff = 0;
            for (PieceType pt = PAWN; pt <= QUEEN; pt = PieceType(int(pt) + 1))
                mat_diff += PIECE_VALUES[pt] * (popcount(board.pieces(WHITE, pt)) - popcount(board.pieces(BLACK, pt)));
            // Adjust mat_diff for advanced passed pawns (imminent promotion is real material).
            {
                Bitboard wpp = board.pieces(WHITE, PAWN);
                Bitboard bpp = board.pieces(BLACK, PAWN);
                Bitboard tmp = wpp;
                while (tmp)
                {
                    Square sq = pop_lsb(tmp);
                    if (!(bpp & passed_pawn_mask(WHITE, sq)))
                        mat_diff += g_eval_params.passed_pawn_eg_bonus[rank_of(sq)];
                }
                tmp = bpp;
                while (tmp)
                {
                    Square sq = pop_lsb(tmp);
                    if (!(wpp & passed_pawn_mask(BLACK, sq)))
                        mat_diff -= g_eval_params.passed_pawn_eg_bonus[RANK_8 - rank_of(sq)];
                }
            }

            int phase = game_phase(board);
            if (phase < 64)
            { // Deep endgame
                Square wk = board.king_square(WHITE);
                Square bk = board.king_square(BLACK);
                // Manhattan distance between kings (encourages winning king to approach)
                int king_dist = std::abs(file_of(wk) - file_of(bk)) + std::abs(rank_of(wk) - rank_of(bk));
                // Corner distance for the losing king (corner = most restrictive)
                auto corner_dist = [](Square sq)
                {
                    int f = file_of(sq), r = rank_of(sq);
                    return std::min({f, 7 - f}) + std::min({r, 7 - r});
                };
                if (mat_diff > g_eval_params.mopup_material_threshold)
                {
                    // White is winning: drive black king to corner, approach with white king
                    eg_score += (7 - corner_dist(bk)) * g_eval_params.mopup_corner_weight;
                    eg_score += (14 - king_dist) * g_eval_params.mopup_distance_weight;
                }
                else if (mat_diff < -g_eval_params.mopup_material_threshold)
                {
                    // Black is winning
                    eg_score -= (7 - corner_dist(wk)) * g_eval_params.mopup_corner_weight;
                    eg_score -= (14 - king_dist) * g_eval_params.mopup_distance_weight;
                }
            }
        }

        // Tempo bonus: small bonus for side to move (initiative)
        if (board.side_to_move() == WHITE)
            mg_score += g_eval_params.tempo_bonus;
        else
            mg_score -= g_eval_params.tempo_bonus;

        // Blend middlegame and endgame scores
        int phase = game_phase(board);
        int score = (mg_score * phase + eg_score * (256 - phase)) / 256;

        // Endgame draw scaling
        int sf = compute_scale_factor(board, score, phase);
        score = score * sf / 256;

        // Return from perspective of side to move
        return board.side_to_move() == WHITE ? score : -score;
    }

    // ============================================================================
    // evaluate_trace() — same logic as evaluate() but tracks each eval term
    // separately and returns a structured EvalTrace.  No I/O.
    // ============================================================================

    EvalTrace evaluate_trace(const Board &board)
    {
        EvalTrace tr;

        // Alias local names that match the old T_* identifiers for minimal diff
        constexpr int T_MATERIAL_PST = EVAL_MATERIAL_PST;
        constexpr int T_BISHOP_PAIR = EVAL_BISHOP_PAIR;
        constexpr int T_ROOK_FILES = EVAL_ROOK_FILES;
        constexpr int T_PAWN_STRUCTURE = EVAL_PAWN_STRUCTURE;
        constexpr int T_MOBILITY = EVAL_MOBILITY;
        constexpr int T_ROOK_7TH = EVAL_ROOK_7TH;
        constexpr int T_OUTPOSTS = EVAL_OUTPOSTS;
        constexpr int T_PINS = EVAL_PINS;
        constexpr int T_PIN_CREATION = EVAL_PIN_CREATION;
        constexpr int T_BAD_BISHOP = EVAL_BAD_BISHOP;
        constexpr int T_THREATS = EVAL_THREATS;
        constexpr int T_SPACE = EVAL_SPACE;
        constexpr int T_ROOK_BEHIND_PASSER = EVAL_ROOK_BEHIND_PASSER;
        constexpr int T_KING_PASSER_DIST = EVAL_KING_PASSER_DIST;
        constexpr int T_WEAK_MINOR = EVAL_WEAK_MINOR;
        constexpr int T_KING_SAFETY = EVAL_KING_SAFETY;
        constexpr int T_CASTLING = EVAL_CASTLING;
        constexpr int T_MOPUP = EVAL_MOPUP;
        constexpr int T_TEMPO = EVAL_TEMPO;

        // Use a macro so the ~700 lines of eval body remain unchanged:
        // `terms[T_FOO].mg` becomes `tr.terms[T_FOO].mg`
        auto &terms = tr.terms;

        int mg_score = 0, eg_score = 0;

        // --- Material + PST ---
        {
            int mg = 0, eg = 0;
            for (PieceType pt = PAWN; pt <= QUEEN; pt = PieceType(int(pt) + 1))
            {
                Bitboard white_pieces = board.pieces(WHITE, pt);
                Bitboard black_pieces = board.pieces(BLACK, pt);
                while (white_pieces)
                {
                    Square s = pop_lsb(white_pieces);
                    mg += PIECE_VALUES[pt] + PST_MG[pt][mirror(s)];
                    eg += PIECE_VALUES[pt] + PST_EG[pt][mirror(s)];
                }
                while (black_pieces)
                {
                    Square s = pop_lsb(black_pieces);
                    mg -= PIECE_VALUES[pt] + PST_MG[pt][s];
                    eg -= PIECE_VALUES[pt] + PST_EG[pt][s];
                }
            }
            {
                Square wk = board.king_square(WHITE);
                Square bk = board.king_square(BLACK);
                mg += PST_MG[KING][mirror(wk)] - PST_MG[KING][bk];
                eg += PST_EG[KING][mirror(wk)] - PST_EG[KING][bk];
            }
            terms[T_MATERIAL_PST].mg = mg;
            terms[T_MATERIAL_PST].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Bishop pair ---
        {
            int mg = 0, eg = 0;
            if (popcount(board.pieces(WHITE, BISHOP)) >= 2)
            {
                mg += g_eval_params.bishop_pair_bonus;
                eg += g_eval_params.bishop_pair_eg_bonus;
            }
            if (popcount(board.pieces(BLACK, BISHOP)) >= 2)
            {
                mg -= g_eval_params.bishop_pair_bonus;
                eg -= g_eval_params.bishop_pair_eg_bonus;
            }
            terms[T_BISHOP_PAIR].mg = mg;
            terms[T_BISHOP_PAIR].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Rook open/semi-open files ---
        {
            int mg = 0, eg = 0;
            for (File f = FILE_A; f < FILE_NB; ++f)
            {
                Bitboard file_mask = FILE_BB[f];
                bool white_pawns = board.pieces(WHITE, PAWN) & file_mask;
                bool black_pawns = board.pieces(BLACK, PAWN) & file_mask;
                if (board.pieces(WHITE, ROOK) & file_mask)
                {
                    if (!white_pawns && !black_pawns)
                    {
                        mg += g_eval_params.rook_open_file_bonus;
                        eg += g_eval_params.rook_open_file_eg;
                    }
                    else if (!white_pawns)
                    {
                        mg += g_eval_params.rook_semi_open_bonus;
                        eg += g_eval_params.rook_semi_open_eg;
                    }
                }
                if (board.pieces(BLACK, ROOK) & file_mask)
                {
                    if (!white_pawns && !black_pawns)
                    {
                        mg -= g_eval_params.rook_open_file_bonus;
                        eg -= g_eval_params.rook_open_file_eg;
                    }
                    else if (!black_pawns)
                    {
                        mg -= g_eval_params.rook_semi_open_bonus;
                        eg -= g_eval_params.rook_semi_open_eg;
                    }
                }
            }
            terms[T_ROOK_FILES].mg = mg;
            terms[T_ROOK_FILES].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Pawn structure ---
        {
            Bitboard wp = board.pieces(WHITE, PAWN);
            Bitboard bp = board.pieces(BLACK, PAWN);
            int pawn_mg = 0, pawn_eg = 0;
            // Always compute from scratch for explain (skip pawn hash)
            Bitboard white_temp = wp;
            while (white_temp)
            {
                Square s = pop_lsb(white_temp);
                File f = file_of(s);
                Rank r = rank_of(s);
                if (popcount(wp & FILE_BB[f]) > 1)
                    pawn_mg += g_eval_params.doubled_pawn_penalty;
                if (!(wp & ADJACENT_FILES[f]))
                    pawn_mg += g_eval_params.isolated_pawn_penalty;
                if (r < RANK_7)
                {
                    Bitboard adj = ADJACENT_FILES[f];
                    Bitboard at_or_behind = 0;
                    for (Rank rr = RANK_1; rr <= r; rr = Rank(int(rr) + 1))
                        at_or_behind |= RANK_BB[rr];
                    if (!(wp & adj & at_or_behind))
                    {
                        Square stop = Square(int(s) + 8);
                        if (pawn_attacks(WHITE, stop) & bp)
                        {
                            pawn_mg += g_eval_params.backward_pawn_penalty_mg;
                            pawn_eg += g_eval_params.backward_pawn_penalty_eg;
                        }
                    }
                }
                bool is_conn_w = (pawn_attacks(BLACK, s) & wp) != 0;
                if (is_conn_w)
                {
                    pawn_mg += g_eval_params.connected_pawn_bonus_mg;
                    pawn_eg += g_eval_params.connected_pawn_bonus_eg;
                }
                if (!(bp & passed_pawn_mask(WHITE, s)))
                {
                    pawn_mg += g_eval_params.passed_pawn_bonus[r];
                    pawn_eg += g_eval_params.passed_pawn_eg_bonus[r];
                    if (!is_conn_w && (pawn_attacks(BLACK, s) & wp))
                    {
                        pawn_mg += g_eval_params.protected_passer_mg;
                        pawn_eg += g_eval_params.protected_passer_eg;
                    }
                }
                else
                {
                    Bitboard blockers = bp & passed_pawn_mask(WHITE, s);
                    if (popcount(blockers) == 1)
                    {
                        pawn_mg += g_eval_params.candidate_passer_mg;
                        pawn_eg += g_eval_params.candidate_passer_eg;
                    }
                }
            }
            Bitboard black_temp = bp;
            while (black_temp)
            {
                Square s = pop_lsb(black_temp);
                File f = file_of(s);
                Rank r = Rank(RANK_8 - rank_of(s));
                if (popcount(bp & FILE_BB[f]) > 1)
                    pawn_mg -= g_eval_params.doubled_pawn_penalty;
                if (!(bp & ADJACENT_FILES[f]))
                    pawn_mg -= g_eval_params.isolated_pawn_penalty;
                {
                    Rank abs_r = rank_of(s);
                    if (abs_r > RANK_2)
                    {
                        Bitboard adj = ADJACENT_FILES[f];
                        Bitboard at_or_behind = 0;
                        for (Rank rr = abs_r; rr < RANK_NB; rr = Rank(int(rr) + 1))
                            at_or_behind |= RANK_BB[rr];
                        if (!(bp & adj & at_or_behind))
                        {
                            Square stop = Square(int(s) - 8);
                            if (pawn_attacks(BLACK, stop) & wp)
                            {
                                pawn_mg -= g_eval_params.backward_pawn_penalty_mg;
                                pawn_eg -= g_eval_params.backward_pawn_penalty_eg;
                            }
                        }
                    }
                }
                bool is_conn_b = (pawn_attacks(WHITE, s) & bp) != 0;
                if (is_conn_b)
                {
                    pawn_mg -= g_eval_params.connected_pawn_bonus_mg;
                    pawn_eg -= g_eval_params.connected_pawn_bonus_eg;
                }
                if (!(wp & passed_pawn_mask(BLACK, s)))
                {
                    pawn_mg -= g_eval_params.passed_pawn_bonus[r];
                    pawn_eg -= g_eval_params.passed_pawn_eg_bonus[r];
                    if (!is_conn_b && (pawn_attacks(WHITE, s) & bp))
                    {
                        pawn_mg -= g_eval_params.protected_passer_mg;
                        pawn_eg -= g_eval_params.protected_passer_eg;
                    }
                }
                else
                {
                    Bitboard blockers = wp & passed_pawn_mask(BLACK, s);
                    if (popcount(blockers) == 1)
                    {
                        pawn_mg -= g_eval_params.candidate_passer_mg;
                        pawn_eg -= g_eval_params.candidate_passer_eg;
                    }
                }
            }
            terms[T_PAWN_STRUCTURE].mg = pawn_mg;
            terms[T_PAWN_STRUCTURE].eg = pawn_eg;
            mg_score += pawn_mg;
            eg_score += pawn_eg;
        }

        // --- Mobility ---
        // Precompute pawn attacks early for safe mobility
        Bitboard white_pawn_attacks_bb = shift<NORTH_EAST>(board.pieces(WHITE, PAWN)) | shift<NORTH_WEST>(board.pieces(WHITE, PAWN));
        Bitboard black_pawn_attacks_bb = shift<SOUTH_EAST>(board.pieces(BLACK, PAWN)) | shift<SOUTH_WEST>(board.pieces(BLACK, PAWN));
        {
            int mg = 0, eg = 0;
            Bitboard occupied = board.pieces();
            Bitboard white_targets = ~board.pieces(WHITE);
            Bitboard black_targets = ~board.pieces(BLACK);

            // Safe squares for minor pieces: exclude enemy-pawn-attacked squares
            Bitboard white_safe = white_targets & ~black_pawn_attacks_bb;
            Bitboard black_safe = black_targets & ~white_pawn_attacks_bb;

            Bitboard knights = board.pieces(WHITE, KNIGHT);
            while (knights)
            {
                Square s = pop_lsb(knights);
                int m = popcount(knight_attacks(s) & white_safe);
                mg += m * g_eval_params.knight_mobility_mg;
                eg += m * g_eval_params.knight_mobility_eg;
            }
            knights = board.pieces(BLACK, KNIGHT);
            while (knights)
            {
                Square s = pop_lsb(knights);
                int m = popcount(knight_attacks(s) & black_safe);
                mg -= m * g_eval_params.knight_mobility_mg;
                eg -= m * g_eval_params.knight_mobility_eg;
            }

            Bitboard bishops = board.pieces(WHITE, BISHOP);
            while (bishops)
            {
                Square s = pop_lsb(bishops);
                int m = popcount(bishop_attacks(s, occupied) & white_safe);
                mg += m * g_eval_params.bishop_mobility_mg;
                eg += m * g_eval_params.bishop_mobility_eg;
            }
            bishops = board.pieces(BLACK, BISHOP);
            while (bishops)
            {
                Square s = pop_lsb(bishops);
                int m = popcount(bishop_attacks(s, occupied) & black_safe);
                mg -= m * g_eval_params.bishop_mobility_mg;
                eg -= m * g_eval_params.bishop_mobility_eg;
            }

            Bitboard rooks = board.pieces(WHITE, ROOK);
            while (rooks)
            {
                Square s = pop_lsb(rooks);
                int m = popcount(rook_attacks(s, occupied) & white_targets);
                mg += m * g_eval_params.rook_mobility_mg;
                eg += m * g_eval_params.rook_mobility_eg;
            }
            rooks = board.pieces(BLACK, ROOK);
            while (rooks)
            {
                Square s = pop_lsb(rooks);
                int m = popcount(rook_attacks(s, occupied) & black_targets);
                mg -= m * g_eval_params.rook_mobility_mg;
                eg -= m * g_eval_params.rook_mobility_eg;
            }

            Bitboard queens = board.pieces(WHITE, QUEEN);
            while (queens)
            {
                Square s = pop_lsb(queens);
                int m = popcount(queen_attacks(s, occupied) & white_targets);
                mg += m * g_eval_params.queen_mobility_mg;
                eg += m * g_eval_params.queen_mobility_eg;
            }
            queens = board.pieces(BLACK, QUEEN);
            while (queens)
            {
                Square s = pop_lsb(queens);
                int m = popcount(queen_attacks(s, occupied) & black_targets);
                mg -= m * g_eval_params.queen_mobility_mg;
                eg -= m * g_eval_params.queen_mobility_eg;
            }

            terms[T_MOBILITY].mg = mg;
            terms[T_MOBILITY].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Rook on 7th rank + connected rooks ---
        {
            int mg = 0, eg = 0;
            Bitboard occupied = board.pieces();
            Bitboard wr = board.pieces(WHITE, ROOK);
            Bitboard wr_copy = wr;
            while (wr_copy)
            {
                Square s = pop_lsb(wr_copy);
                if (rank_of(s) == RANK_7 &&
                    (rank_of(board.king_square(BLACK)) == RANK_8 || (board.pieces(BLACK, PAWN) & RANK_7_BB)))
                {
                    mg += g_eval_params.rook_seventh_mg;
                    eg += g_eval_params.rook_seventh_eg;
                }
            }
            if (popcount(wr) >= 2)
            {
                Bitboard rooks_temp = wr;
                Square r1 = pop_lsb(rooks_temp);
                Square r2 = pop_lsb(rooks_temp);
                if (rook_attacks(r1, occupied) & square_bb(r2))
                    mg += g_eval_params.connected_rooks_bonus;
            }
            Bitboard br = board.pieces(BLACK, ROOK);
            Bitboard br_copy = br;
            while (br_copy)
            {
                Square s = pop_lsb(br_copy);
                if (rank_of(s) == RANK_2 &&
                    (rank_of(board.king_square(WHITE)) == RANK_1 || (board.pieces(WHITE, PAWN) & RANK_2_BB)))
                {
                    mg -= g_eval_params.rook_seventh_mg;
                    eg -= g_eval_params.rook_seventh_eg;
                }
            }
            if (popcount(br) >= 2)
            {
                Bitboard rooks_temp = br;
                Square r1 = pop_lsb(rooks_temp);
                Square r2 = pop_lsb(rooks_temp);
                if (rook_attacks(r1, occupied) & square_bb(r2))
                    mg -= g_eval_params.connected_rooks_bonus;
            }
            terms[T_ROOK_7TH].mg = mg;
            terms[T_ROOK_7TH].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Outposts ---
        {
            int mg = 0, eg = 0;
            Bitboard wp = board.pieces(WHITE, PAWN);
            Bitboard bp = board.pieces(BLACK, PAWN);
            Bitboard white_outpost_ranks = RANK_4_BB | RANK_5_BB | RANK_6_BB;
            Bitboard wn = board.pieces(WHITE, KNIGHT);
            while (wn)
            {
                Square s = pop_lsb(wn);
                if (!(square_bb(s) & white_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
                    enemy_front |= RANK_BB[rr];
                if (!(bp & ADJACENT_FILES[f] & enemy_front))
                {
                    if (pawn_attacks(BLACK, s) & wp)
                    {
                        mg += g_eval_params.knight_outpost_supported_mg;
                        eg += g_eval_params.knight_outpost_supported_eg;
                    }
                    else
                    {
                        mg += g_eval_params.knight_outpost_mg;
                        eg += g_eval_params.knight_outpost_eg;
                    }
                }
            }
            Bitboard wb = board.pieces(WHITE, BISHOP);
            while (wb)
            {
                Square s = pop_lsb(wb);
                if (!(square_bb(s) & white_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
                    enemy_front |= RANK_BB[rr];
                if (!(bp & ADJACENT_FILES[f] & enemy_front))
                {
                    if (pawn_attacks(BLACK, s) & wp)
                    {
                        mg += g_eval_params.bishop_outpost_supported_mg;
                        eg += g_eval_params.bishop_outpost_supported_eg;
                    }
                    else
                    {
                        mg += g_eval_params.bishop_outpost_mg;
                    }
                }
            }
            Bitboard black_outpost_ranks = RANK_3_BB | RANK_4_BB | RANK_5_BB;
            Bitboard bn = board.pieces(BLACK, KNIGHT);
            while (bn)
            {
                Square s = pop_lsb(bn);
                if (!(square_bb(s) & black_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) - 1); rr >= RANK_1; rr = Rank(int(rr) - 1))
                    enemy_front |= RANK_BB[rr];
                if (!(wp & ADJACENT_FILES[f] & enemy_front))
                {
                    if (pawn_attacks(WHITE, s) & bp)
                    {
                        mg -= g_eval_params.knight_outpost_supported_mg;
                        eg -= g_eval_params.knight_outpost_supported_eg;
                    }
                    else
                    {
                        mg -= g_eval_params.knight_outpost_mg;
                        eg -= g_eval_params.knight_outpost_eg;
                    }
                }
            }
            Bitboard bb = board.pieces(BLACK, BISHOP);
            while (bb)
            {
                Square s = pop_lsb(bb);
                if (!(square_bb(s) & black_outpost_ranks))
                    continue;
                File f = file_of(s);
                Bitboard enemy_front = 0;
                for (Rank rr = Rank(rank_of(s) - 1); rr >= RANK_1; rr = Rank(int(rr) - 1))
                    enemy_front |= RANK_BB[rr];
                if (!(wp & ADJACENT_FILES[f] & enemy_front))
                {
                    if (pawn_attacks(WHITE, s) & bp)
                    {
                        mg -= g_eval_params.bishop_outpost_supported_mg;
                        eg -= g_eval_params.bishop_outpost_supported_eg;
                    }
                    else
                    {
                        mg -= g_eval_params.bishop_outpost_mg;
                    }
                }
            }
            terms[T_OUTPOSTS].mg = mg;
            terms[T_OUTPOSTS].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Pins ---
        {
            int mg = 0, eg = 0;
            Bitboard occupied = board.pieces();
            for (Color c : {WHITE, BLACK})
            {
                Color them = ~c;
                Square ksq = board.king_square(c);
                Bitboard our = board.pieces(c);
                Bitboard rq = board.pieces(them, ROOK, QUEEN);
                Bitboard bq = board.pieces(them, BISHOP, QUEEN);
                Bitboard snipers = (rook_attacks(ksq, 0) & rq) | (bishop_attacks(ksq, 0) & bq);
                Bitboard pinned = 0;
                while (snipers)
                {
                    Square s = pop_lsb(snipers);
                    Bitboard between = BETWEEN_BB[ksq][s] & occupied;
                    if (!more_than_one(between))
                        pinned |= between & our;
                }
                int pin_count = popcount(pinned);
                if (c == WHITE)
                {
                    mg += pin_count * g_eval_params.pinned_piece_penalty_mg;
                    eg += pin_count * g_eval_params.pinned_piece_penalty_eg;
                }
                else
                {
                    mg -= pin_count * g_eval_params.pinned_piece_penalty_mg;
                    eg -= pin_count * g_eval_params.pinned_piece_penalty_eg;
                }
            }
            terms[T_PINS].mg = mg;
            terms[T_PINS].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Pin creation ---
        {
            static constexpr int PV[] = {0, 100, 320, 330, 500, 900, 20000};
            int mg = 0, eg = 0;
            Bitboard occupied = board.pieces();
            for (Color c : {WHITE, BLACK})
            {
                Color them = ~c;
                int bonus = 0;
                Bitboard our_diag = board.pieces(c, BISHOP) | board.pieces(c, QUEEN);
                while (our_diag)
                {
                    Square s = pop_lsb(our_diag);
                    Bitboard atts = bishop_attacks(s, occupied);
                    Bitboard victims = atts & board.pieces(them);
                    while (victims)
                    {
                        Square v = pop_lsb(victims);
                        Bitboard xray = bishop_attacks(s, occupied ^ square_bb(v));
                        Bitboard behind = (xray & ~atts) & board.pieces(them);
                        while (behind)
                        {
                            Square b = pop_lsb(behind);
                            PieceType bpt = type_of(board.piece_on(b));
                            PieceType vpt = type_of(board.piece_on(v));
                            if ((bpt == QUEEN || bpt == KING) && PV[bpt] > PV[vpt])
                                bonus++;
                        }
                    }
                }
                Bitboard our_orth = board.pieces(c, ROOK) | board.pieces(c, QUEEN);
                while (our_orth)
                {
                    Square s = pop_lsb(our_orth);
                    Bitboard atts = rook_attacks(s, occupied);
                    Bitboard victims = atts & board.pieces(them);
                    while (victims)
                    {
                        Square v = pop_lsb(victims);
                        Bitboard xray = rook_attacks(s, occupied ^ square_bb(v));
                        Bitboard behind = (xray & ~atts) & board.pieces(them);
                        while (behind)
                        {
                            Square b = pop_lsb(behind);
                            PieceType bpt = type_of(board.piece_on(b));
                            PieceType vpt = type_of(board.piece_on(v));
                            if ((bpt == QUEEN || bpt == KING) && PV[bpt] > PV[vpt])
                                bonus++;
                        }
                    }
                }
                if (c == WHITE)
                {
                    mg += bonus * g_eval_params.pin_creation_bonus_mg;
                    eg += bonus * g_eval_params.pin_creation_bonus_eg;
                }
                else
                {
                    mg -= bonus * g_eval_params.pin_creation_bonus_mg;
                    eg -= bonus * g_eval_params.pin_creation_bonus_eg;
                }
            }
            terms[T_PIN_CREATION].mg = mg;
            terms[T_PIN_CREATION].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Bad bishop ---
        {
            int mg = 0, eg = 0;
            Bitboard wb = board.pieces(WHITE, BISHOP);
            while (wb)
            {
                Square s = pop_lsb(wb);
                Bitboard cc = (square_bb(s) & DARK_SQUARES) ? DARK_SQUARES : LIGHT_SQUARES;
                int n = popcount(board.pieces(WHITE, PAWN) & cc);
                mg += n * g_eval_params.bad_bishop_per_pawn_mg;
                eg += n * g_eval_params.bad_bishop_per_pawn_eg;
            }
            Bitboard bb_b = board.pieces(BLACK, BISHOP);
            while (bb_b)
            {
                Square s = pop_lsb(bb_b);
                Bitboard cc = (square_bb(s) & DARK_SQUARES) ? DARK_SQUARES : LIGHT_SQUARES;
                int n = popcount(board.pieces(BLACK, PAWN) & cc);
                mg -= n * g_eval_params.bad_bishop_per_pawn_mg;
                eg -= n * g_eval_params.bad_bishop_per_pawn_eg;
            }
            terms[T_BAD_BISHOP].mg = mg;
            terms[T_BAD_BISHOP].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Threats ---
        {
            int mg = 0, eg = 0;
            Bitboard occupied = board.pieces();
            Bitboard black_non_pawns = board.pieces(BLACK, KNIGHT) | board.pieces(BLACK, BISHOP) | board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN);
            int wpt = popcount(white_pawn_attacks_bb & black_non_pawns);
            mg += wpt * g_eval_params.threat_by_pawn_mg;
            eg += wpt * g_eval_params.threat_by_pawn_eg;

            Bitboard black_majors = board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN);
            Bitboard w_minor_att = 0;
            Bitboard wn = board.pieces(WHITE, KNIGHT);
            while (wn)
                w_minor_att |= knight_attacks(pop_lsb(wn));
            Bitboard wbishops = board.pieces(WHITE, BISHOP);
            while (wbishops)
                w_minor_att |= bishop_attacks(pop_lsb(wbishops), occupied);
            int wmt = popcount(w_minor_att & black_majors);
            mg += wmt * g_eval_params.threat_by_minor_mg;
            eg += wmt * g_eval_params.threat_by_minor_eg;

            Bitboard w_rook_att = 0;
            Bitboard wr = board.pieces(WHITE, ROOK);
            while (wr)
                w_rook_att |= rook_attacks(pop_lsb(wr), occupied);
            int wrt = popcount(w_rook_att & board.pieces(BLACK, QUEEN));
            mg += wrt * g_eval_params.threat_by_rook_mg;
            eg += wrt * g_eval_params.threat_by_rook_eg;

            Bitboard white_non_pawns = board.pieces(WHITE, KNIGHT) | board.pieces(WHITE, BISHOP) | board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN);
            int bpt = popcount(black_pawn_attacks_bb & white_non_pawns);
            mg -= bpt * g_eval_params.threat_by_pawn_mg;
            eg -= bpt * g_eval_params.threat_by_pawn_eg;
            Bitboard white_majors = board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN);
            Bitboard b_minor_att = 0;
            Bitboard bn = board.pieces(BLACK, KNIGHT);
            while (bn)
                b_minor_att |= knight_attacks(pop_lsb(bn));
            Bitboard bbishops = board.pieces(BLACK, BISHOP);
            while (bbishops)
                b_minor_att |= bishop_attacks(pop_lsb(bbishops), occupied);
            int bmt = popcount(b_minor_att & white_majors);
            mg -= bmt * g_eval_params.threat_by_minor_mg;
            eg -= bmt * g_eval_params.threat_by_minor_eg;
            Bitboard b_rook_att = 0;
            Bitboard br = board.pieces(BLACK, ROOK);
            while (br)
                b_rook_att |= rook_attacks(pop_lsb(br), occupied);
            int brt = popcount(b_rook_att & board.pieces(WHITE, QUEEN));
            mg -= brt * g_eval_params.threat_by_rook_mg;
            eg -= brt * g_eval_params.threat_by_rook_eg;

            terms[T_THREATS].mg = mg;
            terms[T_THREATS].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- Space ---
        {
            constexpr Bitboard CENTER_FILES = FILE_C_BB | FILE_D_BB | FILE_E_BB | FILE_F_BB;
            constexpr Bitboard WHITE_SPACE = CENTER_FILES & (RANK_2_BB | RANK_3_BB | RANK_4_BB);
            constexpr Bitboard BLACK_SPACE = CENTER_FILES & (RANK_5_BB | RANK_6_BB | RANK_7_BB);
            int mg = 0;
            mg += popcount(WHITE_SPACE & ~black_pawn_attacks_bb) * g_eval_params.space_bonus_mg;
            mg -= popcount(BLACK_SPACE & ~white_pawn_attacks_bb) * g_eval_params.space_bonus_mg;
            terms[T_SPACE].mg = mg;
            mg_score += mg;
        }

        // --- Rook behind passer + King-passer distance ---
        {
            Bitboard wp = board.pieces(WHITE, PAWN);
            Bitboard bp = board.pieces(BLACK, PAWN);
            Square wk = board.king_square(WHITE);
            Square bk = board.king_square(BLACK);
            int rbp_mg = 0, rbp_eg = 0, kpd_mg = 0, kpd_eg = 0;

            Bitboard temp = wp;
            while (temp)
            {
                Square s = pop_lsb(temp);
                if (!(bp & passed_pawn_mask(WHITE, s)))
                {
                    File f = file_of(s);
                    Bitboard behind = FILE_BB[f];
                    Bitboard behind_ranks = 0;
                    for (Rank rr = RANK_1; rr < rank_of(s); rr = Rank(int(rr) + 1))
                        behind_ranks |= RANK_BB[rr];
                    if (board.pieces(WHITE, ROOK) & behind & behind_ranks)
                    {
                        rbp_mg += g_eval_params.rook_behind_passer_mg;
                        rbp_eg += g_eval_params.rook_behind_passer_eg;
                    }
                    int wkd = std::abs(file_of(wk) - f) + std::abs(rank_of(wk) - rank_of(s));
                    int bkd = std::abs(file_of(bk) - f) + std::abs(rank_of(bk) - rank_of(s));
                    kpd_mg += (6 - wkd) * g_eval_params.king_passer_support_mg;
                    kpd_mg += bkd * g_eval_params.king_passer_threat_mg;
                    kpd_eg += (6 - wkd) * g_eval_params.king_passer_support_eg;
                    kpd_eg += bkd * g_eval_params.king_passer_threat_eg;
                    // Wrong-colour bishop draw
                    {
                        Bitboard wb = board.pieces(WHITE, BISHOP);
                        if (popcount(wb) == 1 &&
                            !(board.pieces(WHITE, ROOK) | board.pieces(WHITE, QUEEN) | board.pieces(WHITE, KNIGHT)))
                        {
                            bool bishop_on_dark = (wb & DARK_SQUARES) != 0;
                            Square promo = make_square(f, RANK_8);
                            bool promo_on_dark = (square_bb(promo) & DARK_SQUARES) != 0;
                            if (bishop_on_dark != promo_on_dark && (f == FILE_A || f == FILE_H))
                                kpd_eg += g_eval_params.wrong_bishop_passer_penalty_eg;
                        }
                    }
                }
            }
            temp = bp;
            while (temp)
            {
                Square s = pop_lsb(temp);
                if (!(wp & passed_pawn_mask(BLACK, s)))
                {
                    File f = file_of(s);
                    Bitboard behind = FILE_BB[f];
                    Bitboard behind_ranks = 0;
                    for (Rank rr = Rank(rank_of(s) + 1); rr < RANK_NB; rr = Rank(int(rr) + 1))
                        behind_ranks |= RANK_BB[rr];
                    if (board.pieces(BLACK, ROOK) & behind & behind_ranks)
                    {
                        rbp_mg -= g_eval_params.rook_behind_passer_mg;
                        rbp_eg -= g_eval_params.rook_behind_passer_eg;
                    }
                    int bkd = std::abs(file_of(bk) - f) + std::abs(rank_of(bk) - rank_of(s));
                    int wkd = std::abs(file_of(wk) - f) + std::abs(rank_of(wk) - rank_of(s));
                    kpd_mg -= (6 - bkd) * g_eval_params.king_passer_support_mg;
                    kpd_mg -= wkd * g_eval_params.king_passer_threat_mg;
                    kpd_eg -= (6 - bkd) * g_eval_params.king_passer_support_eg;
                    kpd_eg -= wkd * g_eval_params.king_passer_threat_eg;
                    // Wrong-colour bishop draw
                    {
                        Bitboard bb_b = board.pieces(BLACK, BISHOP);
                        if (popcount(bb_b) == 1 &&
                            !(board.pieces(BLACK, ROOK) | board.pieces(BLACK, QUEEN) | board.pieces(BLACK, KNIGHT)))
                        {
                            bool bishop_on_dark = (bb_b & DARK_SQUARES) != 0;
                            Square promo = make_square(f, RANK_1);
                            bool promo_on_dark = (square_bb(promo) & DARK_SQUARES) != 0;
                            if (bishop_on_dark != promo_on_dark && (f == FILE_A || f == FILE_H))
                                kpd_eg -= g_eval_params.wrong_bishop_passer_penalty_eg;
                        }
                    }
                }
            }
            terms[T_ROOK_BEHIND_PASSER].mg = rbp_mg;
            terms[T_ROOK_BEHIND_PASSER].eg = rbp_eg;
            terms[T_KING_PASSER_DIST].mg = kpd_mg;
            terms[T_KING_PASSER_DIST].eg = kpd_eg;
            mg_score += rbp_mg + kpd_mg;
            eg_score += rbp_eg + kpd_eg;
        }

        // --- Weak minor ---
        {
            int mg = 0, eg = 0;
            Bitboard w_minors = board.pieces(WHITE, KNIGHT) | board.pieces(WHITE, BISHOP);
            Bitboard weak_w = (w_minors & ~white_pawn_attacks_bb) & black_pawn_attacks_bb;
            mg += popcount(weak_w) * g_eval_params.weak_minor_penalty_mg;
            eg += popcount(weak_w) * g_eval_params.weak_minor_penalty_eg;
            Bitboard b_minors = board.pieces(BLACK, KNIGHT) | board.pieces(BLACK, BISHOP);
            Bitboard weak_b = (b_minors & ~black_pawn_attacks_bb) & white_pawn_attacks_bb;
            mg -= popcount(weak_b) * g_eval_params.weak_minor_penalty_mg;
            eg -= popcount(weak_b) * g_eval_params.weak_minor_penalty_eg;
            terms[T_WEAK_MINOR].mg = mg;
            terms[T_WEAK_MINOR].eg = eg;
            mg_score += mg;
            eg_score += eg;
        }

        // --- King safety ---
        {
            Bitboard occupied = board.pieces();
            int ks_mg = 0;
            for (Color c : {WHITE, BLACK})
            {
                Color them = ~c;
                Square ksq = board.king_square(c);
                Bitboard king_zone = king_attacks(ksq) | square_bb(ksq);
                int attackers_count = 0, attack_weight = 0;

                Bitboard en = board.pieces(them, KNIGHT);
                while (en)
                {
                    if (knight_attacks(pop_lsb(en)) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_knight;
                    }
                }
                Bitboard eb = board.pieces(them, BISHOP);
                while (eb)
                {
                    if (bishop_attacks(pop_lsb(eb), occupied) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_bishop;
                    }
                }
                Bitboard er = board.pieces(them, ROOK);
                while (er)
                {
                    if (rook_attacks(pop_lsb(er), occupied) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_rook;
                    }
                }
                Bitboard eq = board.pieces(them, QUEEN);
                while (eq)
                {
                    if (queen_attacks(pop_lsb(eq), occupied) & king_zone)
                    {
                        attackers_count++;
                        attack_weight += g_eval_params.king_attacker_weight_queen;
                    }
                }

                int safety_penalty = 0;
                if (attack_weight > 0)
                    safety_penalty = SAFETY_TABLE[std::min(attack_weight, 99)];

                File kf = file_of(ksq);
                Bitboard shield_files = FILE_BB[kf];
                if (kf > FILE_A)
                    shield_files |= FILE_BB[kf - 1];
                if (kf < FILE_H)
                    shield_files |= FILE_BB[kf + 1];
                Bitboard shield;
                if (c == WHITE)
                {
                    Rank kr = rank_of(ksq);
                    Bitboard sr = 0;
                    if (kr < RANK_8)
                        sr |= RANK_BB[kr + 1];
                    if (kr + 1 < RANK_8)
                        sr |= RANK_BB[kr + 2];
                    shield = board.pieces(WHITE, PAWN) & shield_files & sr;
                }
                else
                {
                    Rank kr = rank_of(ksq);
                    Bitboard sr = 0;
                    if (kr > RANK_1)
                        sr |= RANK_BB[kr - 1];
                    if (kr - 1 > RANK_1)
                        sr |= RANK_BB[kr - 2];
                    shield = board.pieces(BLACK, PAWN) & shield_files & sr;
                }
                int shield_bonus = popcount(shield) * g_eval_params.pawn_shield_bonus;

                int open_file_penalty = 0;
                for (File f = std::max(FILE_A, File(kf - 1)); f <= std::min(FILE_H, File(kf + 1)); ++f)
                {
                    if (!(board.pieces(c, PAWN) & FILE_BB[f]))
                    {
                        open_file_penalty += g_eval_params.king_open_file_penalty;
                        if (!(board.pieces(them, PAWN) & FILE_BB[f]))
                            open_file_penalty += g_eval_params.king_open_file_full_extra;
                    }
                }

                int king_score = -safety_penalty + shield_bonus - open_file_penalty;
                if (c == WHITE)
                    ks_mg += king_score;
                else
                    ks_mg -= king_score;
            }
            terms[T_KING_SAFETY].mg = ks_mg;
            mg_score += ks_mg;
        }

        // --- Castling urgency ---
        {
            int mg = 0;
            Square wk = board.king_square(WHITE);
            Square bk = board.king_square(BLACK);
            CastlingRight cr = board.castling_rights();
            if (wk == E1 && (cr & (WHITE_OO | WHITE_OOO)))
                mg -= g_eval_params.castling_urgency_penalty;
            if (bk == E8 && (cr & (BLACK_OO | BLACK_OOO)))
                mg += g_eval_params.castling_urgency_penalty;
            // Castled bonus
            if (wk == G1 || wk == C1)
                mg += g_eval_params.castled_bonus_mg;
            if (bk == G8 || bk == C8)
                mg -= g_eval_params.castled_bonus_mg;
            terms[T_CASTLING].mg = mg;
            mg_score += mg;
        }

        // --- Mopup ---
        {
            int mat_diff = 0;
            for (PieceType pt = PAWN; pt <= QUEEN; pt = PieceType(int(pt) + 1))
                mat_diff += PIECE_VALUES[pt] * (popcount(board.pieces(WHITE, pt)) - popcount(board.pieces(BLACK, pt)));
            // Adjust mat_diff for advanced passed pawns (imminent promotion is real material).
            {
                Bitboard wpp = board.pieces(WHITE, PAWN);
                Bitboard bpp = board.pieces(BLACK, PAWN);
                Bitboard tmp = wpp;
                while (tmp)
                {
                    Square sq = pop_lsb(tmp);
                    if (!(bpp & passed_pawn_mask(WHITE, sq)))
                        mat_diff += g_eval_params.passed_pawn_eg_bonus[rank_of(sq)];
                }
                tmp = bpp;
                while (tmp)
                {
                    Square sq = pop_lsb(tmp);
                    if (!(wpp & passed_pawn_mask(BLACK, sq)))
                        mat_diff -= g_eval_params.passed_pawn_eg_bonus[RANK_8 - rank_of(sq)];
                }
            }
            int phase = game_phase(board);
            int eg = 0;
            if (phase < 64)
            {
                Square wk = board.king_square(WHITE);
                Square bk = board.king_square(BLACK);
                int king_dist = std::abs(file_of(wk) - file_of(bk)) + std::abs(rank_of(wk) - rank_of(bk));
                auto corner_dist = [](Square sq)
                { int f = int(file_of(sq)), r = int(rank_of(sq)); return std::min(f, 7-f) + std::min(r, 7-r); };
                if (mat_diff > g_eval_params.mopup_material_threshold)
                {
                    eg += (7 - corner_dist(bk)) * g_eval_params.mopup_corner_weight;
                    eg += (14 - king_dist) * g_eval_params.mopup_distance_weight;
                }
                else if (mat_diff < -g_eval_params.mopup_material_threshold)
                {
                    eg -= (7 - corner_dist(wk)) * g_eval_params.mopup_corner_weight;
                    eg -= (14 - king_dist) * g_eval_params.mopup_distance_weight;
                }
            }
            terms[T_MOPUP].eg = eg;
            eg_score += eg;
        }

        // --- Tempo ---
        {
            int mg = 0;
            if (board.side_to_move() == WHITE)
                mg += g_eval_params.tempo_bonus;
            else
                mg -= g_eval_params.tempo_bonus;
            terms[T_TEMPO].mg = mg;
            mg_score += mg;
        }

        // Fill trace summary fields
        tr.phase = game_phase(board);
        tr.mg_total = mg_score;
        tr.eg_total = eg_score;
        tr.blended = (mg_score * tr.phase + eg_score * (256 - tr.phase)) / 256;

        // Endgame draw scaling
        int sf = compute_scale_factor(board, tr.blended, tr.phase);
        tr.scale_factor = sf;
        tr.blended = tr.blended * sf / 256;
        tr.stm_score = board.side_to_move() == WHITE ? tr.blended : -tr.blended;

        return tr;
    }

    // ============================================================================
    // evaluate_explain() — delegates to evaluate_trace(), then prints table.
    // ============================================================================

    int evaluate_explain(const Board &board)
    {
        static const char *TERM_NAMES[EVAL_TERM_COUNT] = {
            "Material + PST",
            "Bishop pair",
            "Rook open files",
            "Pawn structure",
            "Mobility",
            "Rook on 7th / connected",
            "Outposts",
            "Pin penalty",
            "Pin creation",
            "Bad bishop",
            "Threats",
            "Space",
            "Rook behind passer",
            "King-passer distance",
            "Weak minor",
            "King safety",
            "Castling urgency",
            "Mopup",
            "Tempo",
        };

        EvalTrace tr = evaluate_trace(board);

        std::cout << "\n";
        std::cout << "  Side to move: " << (board.side_to_move() == WHITE ? "White" : "Black") << "\n";
        std::cout << "  Game phase:   " << tr.phase << "/256  ("
                  << (tr.phase > 192 ? "opening" : tr.phase > 96 ? "middlegame"
                                               : tr.phase > 32   ? "late MG/early EG"
                                                                 : "endgame")
                  << ")\n\n";

        std::cout << "  +--------------------------+-------+-------+--------+\n";
        std::cout << "  | Term                     |   MG  |   EG  | Blended|\n";
        std::cout << "  +--------------------------+-------+-------+--------+\n";

        for (int i = 0; i < EVAL_TERM_COUNT; ++i)
        {
            if (tr.terms[i].mg == 0 && tr.terms[i].eg == 0)
                continue;
            char buf[128];
            std::snprintf(buf, sizeof(buf), "  | %-24s | %+5d | %+5d | %+6d |",
                          TERM_NAMES[i], tr.terms[i].mg, tr.terms[i].eg, tr.blend(i));
            std::cout << buf << "\n";
        }

        std::cout << "  +--------------------------+-------+-------+--------+\n";
        {
            char buf[128];
            std::snprintf(buf, sizeof(buf), "  | %-24s | %+5d | %+5d | %+6d |",
                          "TOTAL (White POV)", tr.mg_total, tr.eg_total, tr.blended);
            std::cout << buf << "\n";
        }
        std::cout << "  +--------------------------+-------+-------+--------+\n";
        if (tr.scale_factor != 256)
        {
            std::cout << "  Scale factor: " << tr.scale_factor << "/256 ("
                      << (tr.scale_factor * 100 / 256) << "%)\n";
        }
        std::cout << "  Score (side-to-move POV): " << tr.stm_score << " cp\n";

        return tr.stm_score;
    }

} // namespace Chess
