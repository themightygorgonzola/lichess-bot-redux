// ============================================================================
// NNUE Updater v2 — incremental accumulator update (HalfKAv2)
//
// Key difference from v1: king moves force a full refresh because the
// king bucket (= oriented king square) changes, invalidating ALL feature
// indices for that perspective. Non-king moves use FUSED updates: a single
// AVX2 pass reads the parent accumulator and writes the child, eliminating
// the 4KB memcpy.
// ============================================================================

#include "nnue_updater.h"
#include "nnue_eval.h"
#include "nnue_network.h"
#include "../board.h"
#include <cstring>
#ifdef PROFILE
#ifdef _MSC_VER
#include <intrin.h>
#else
#include <x86intrin.h>
#endif
#endif

namespace Chess::NNUE
{

#ifdef PROFILE
    static inline uint64_t profile_cycles_now()
    {
        return __rdtsc();
    }
#else
    static inline uint64_t profile_cycles_now()
    {
        return 0;
    }
#endif

    // ============================================================================
    // Primitive add/sub helpers (single perspective) — retained for refresh path
    // ============================================================================

    void acc_add_piece_perspective(const Network &net, Accumulator &acc,
                                   int perspective_idx, int feature_idx)
    {
        net.add_feature(acc, perspective_idx, feature_idx);
    }

    void acc_sub_piece_perspective(const Network &net, Accumulator &acc,
                                   int perspective_idx, int feature_idx)
    {
        net.sub_feature(acc, perspective_idx, feature_idx);
    }

    // ============================================================================
    // Fused do_move update
    //
    // For king moves (including castling where the king moves): full refresh.
    // For non-king moves: compute feature indices per perspective and call
    // apply_update_1a1s (normal) or apply_update_1a2s (capture), which does
    // copy + delta in a single AVX2 pass — no memcpy at all.
    // ============================================================================

    void update_accumulator_do_move(const Network &net,
                                    Accumulator &acc,
                                    const Accumulator &prev,
                                    const Board &board,
                                    Move m, Color side, Piece captured_piece)
    {
        uint64_t t0 = profile_cycles_now();
        Square from = m.from();
        Square to = m.to();
        Piece pc = board.piece_on(to); // piece has already been moved on the board
        PieceType moved_pt = type_of(pc);

        // ── King move? Full refresh. ──
        if (moved_pt == KING || m.type() == CASTLING)
        {
            net.refresh(acc, board);
            nnue_profile_note_do_move_update(true, captured_piece != NO_PIECE, profile_cycles_now() - t0);
            return;
        }

        acc.computed = true;

        // Pre-compute king info for both perspectives (after the move)
        Square w_king = board.king_square(WHITE);
        Square b_king = board.king_square(BLACK);
        PerspectiveInfo w_info = make_perspective_info(w_king, WHITE);
        PerspectiveInfo b_info = make_perspective_info(b_king, BLACK);

        struct PreparedPerspective
        {
            int16_t *dst;
            const int16_t *src;
            int king_oriented;
            int from_sq;
            int to_sq;
            int mover_rel;
            int cap_rel;
        };

        PreparedPerspective prep[2] = {
            {
                acc.values[0],
                prev.values[0],
                w_info.king_oriented,
                orient_square_for_perspective(w_info, WHITE, from),
                orient_square_for_perspective(w_info, WHITE, to),
                (side == WHITE) ? 0 : 1,
                (captured_piece != NO_PIECE && color_of(captured_piece) == WHITE) ? 0 : 1,
            },
            {
                acc.values[1],
                prev.values[1],
                b_info.king_oriented,
                orient_square_for_perspective(b_info, BLACK, from),
                orient_square_for_perspective(b_info, BLACK, to),
                (side == BLACK) ? 0 : 1,
                (captured_piece != NO_PIECE && color_of(captured_piece) == BLACK) ? 0 : 1,
            }};

        const MoveType mt = m.type();
        const bool has_capture = captured_piece != NO_PIECE;

        if (mt == EN_PASSANT)
        {
            Square capsq = Square(int(to) + (side == WHITE ? SOUTH : NORTH));
            const int enemy_rel_white = (BLACK == WHITE) ? 0 : 1;
            const int enemy_rel_black = (WHITE == BLACK) ? 0 : 1;
            const int capsq_w = orient_square_for_perspective(w_info, WHITE, capsq);
            const int capsq_b = orient_square_for_perspective(b_info, BLACK, capsq);

            const int sub1_w = feature_index(prep[0].king_oriented, prep[0].mover_rel, 0, prep[0].from_sq);
            const int add1_w = feature_index(prep[0].king_oriented, prep[0].mover_rel, 0, prep[0].to_sq);
            const int sub2_w = feature_index(prep[0].king_oriented, enemy_rel_white, 0, capsq_w);
            net.apply_update_1a2s(prep[0].dst, prep[0].src, add1_w, sub1_w, sub2_w);
            net.update_psqt_1a2s(acc.psqt[0], prev.psqt[0], add1_w, sub1_w, sub2_w);

            const int sub1_b = feature_index(prep[1].king_oriented, prep[1].mover_rel, 0, prep[1].from_sq);
            const int add1_b = feature_index(prep[1].king_oriented, prep[1].mover_rel, 0, prep[1].to_sq);
            const int sub2_b = feature_index(prep[1].king_oriented, enemy_rel_black, 0, capsq_b);
            net.apply_update_1a2s(prep[1].dst, prep[1].src, add1_b, sub1_b, sub2_b);
            net.update_psqt_1a2s(acc.psqt[1], prev.psqt[1], add1_b, sub1_b, sub2_b);
        }
        else if (mt == PROMOTION)
        {
            const int promo_pt0 = int(m.promotion_type()) - 1;
            const int pawn_pt0 = 0;
            const int cap_pt0 = has_capture ? (int(type_of(captured_piece)) - 1) : 0;

            const int sub1_w = feature_index(prep[0].king_oriented, prep[0].mover_rel, pawn_pt0, prep[0].from_sq);
            const int add1_w = feature_index(prep[0].king_oriented, prep[0].mover_rel, promo_pt0, prep[0].to_sq);
            if (has_capture)
            {
                const int sub2_w = feature_index(prep[0].king_oriented, prep[0].cap_rel, cap_pt0, prep[0].to_sq);
                net.apply_update_1a2s(prep[0].dst, prep[0].src, add1_w, sub1_w, sub2_w);
                net.update_psqt_1a2s(acc.psqt[0], prev.psqt[0], add1_w, sub1_w, sub2_w);
            }
            else
            {
                net.apply_update_1a1s(prep[0].dst, prep[0].src, add1_w, sub1_w);
                net.update_psqt_1a1s(acc.psqt[0], prev.psqt[0], add1_w, sub1_w);
            }

            const int sub1_b = feature_index(prep[1].king_oriented, prep[1].mover_rel, pawn_pt0, prep[1].from_sq);
            const int add1_b = feature_index(prep[1].king_oriented, prep[1].mover_rel, promo_pt0, prep[1].to_sq);
            if (has_capture)
            {
                const int sub2_b = feature_index(prep[1].king_oriented, prep[1].cap_rel, cap_pt0, prep[1].to_sq);
                net.apply_update_1a2s(prep[1].dst, prep[1].src, add1_b, sub1_b, sub2_b);
                net.update_psqt_1a2s(acc.psqt[1], prev.psqt[1], add1_b, sub1_b, sub2_b);
            }
            else
            {
                net.apply_update_1a1s(prep[1].dst, prep[1].src, add1_b, sub1_b);
                net.update_psqt_1a1s(acc.psqt[1], prev.psqt[1], add1_b, sub1_b);
            }
        }
        else
        {
            const int moved_pt0 = int(moved_pt) - 1;
            const int sub1_w = feature_index(prep[0].king_oriented, prep[0].mover_rel, moved_pt0, prep[0].from_sq);
            const int add1_w = feature_index(prep[0].king_oriented, prep[0].mover_rel, moved_pt0, prep[0].to_sq);
            if (has_capture)
            {
                const int cap_pt0 = int(type_of(captured_piece)) - 1;
                const int sub2_w = feature_index(prep[0].king_oriented, prep[0].cap_rel, cap_pt0, prep[0].to_sq);
                net.apply_update_1a2s(prep[0].dst, prep[0].src, add1_w, sub1_w, sub2_w);
                net.update_psqt_1a2s(acc.psqt[0], prev.psqt[0], add1_w, sub1_w, sub2_w);
            }
            else
            {
                net.apply_update_1a1s(prep[0].dst, prep[0].src, add1_w, sub1_w);
                net.update_psqt_1a1s(acc.psqt[0], prev.psqt[0], add1_w, sub1_w);
            }

            const int sub1_b = feature_index(prep[1].king_oriented, prep[1].mover_rel, moved_pt0, prep[1].from_sq);
            const int add1_b = feature_index(prep[1].king_oriented, prep[1].mover_rel, moved_pt0, prep[1].to_sq);
            if (has_capture)
            {
                const int cap_pt0 = int(type_of(captured_piece)) - 1;
                const int sub2_b = feature_index(prep[1].king_oriented, prep[1].cap_rel, cap_pt0, prep[1].to_sq);
                net.apply_update_1a2s(prep[1].dst, prep[1].src, add1_b, sub1_b, sub2_b);
                net.update_psqt_1a2s(acc.psqt[1], prev.psqt[1], add1_b, sub1_b, sub2_b);
            }
            else
            {
                net.apply_update_1a1s(prep[1].dst, prep[1].src, add1_b, sub1_b);
                net.update_psqt_1a1s(acc.psqt[1], prev.psqt[1], add1_b, sub1_b);
            }
        }

        const bool capture_like = (captured_piece != NO_PIECE) || (m.type() == EN_PASSANT) || (m.type() == PROMOTION);
        nnue_profile_note_do_move_update(false, capture_like, profile_cycles_now() - t0);
    }

    // ============================================================================
    // Null move — just copy the parent accumulator
    // ============================================================================

    void update_accumulator_null_move(Accumulator &acc, const Accumulator &prev)
    {
        uint64_t t0 = profile_cycles_now();
        std::memcpy(&acc, &prev, sizeof(Accumulator));
        acc.computed = true;
        nnue_profile_note_null_update(profile_cycles_now() - t0);
    }

    // ============================================================================
    // Full refresh — delegate to Network::refresh
    // ============================================================================

    void update_accumulator_full(const Network &net, Accumulator &acc, const Board &board)
    {
        uint64_t t0 = profile_cycles_now();
        net.refresh(acc, board);
        nnue_profile_note_full_update(profile_cycles_now() - t0);
    }

} // namespace Chess::NNUE
