#pragma once

#include <string>

// ============================================================================
// EvalParams — all tunable scalar constants for the evaluation function.
//
// Design notes:
//   - PSTs (768 ints) and piece values stay constexpr in eval.cpp. They are
//     table-shaped knobs rarely tuned individually via UCI options.
//   - Every *scalar* bonus/penalty lives here with a descriptive name and a
//     sensible default. This makes them:
//       1. Trivially findable / changeable in one place.
//       2. Tweakable at runtime via "setoption name Eval* value N" without
//          rebuilding the engine.
//       3. Ready for SPSA / Texel auto-tuning: serialise to a flat int array,
//          run optimiser, deserialise.
//
// Usage:
//   #include "eval_params.h"
//   // Read:  g_eval_params.knight_outpost_supported_mg
//   // Write: set_eval_params(p);   or   setoption → set_eval_param_by_name()
// ============================================================================

namespace Chess
{

     struct EvalParams
     {

          // -----------------------------------------------------------------------
          // Piece-Square Tables (PSTs) — 12 arrays of 64 ints
          //
          // Convention: arrays are written rank-8-first (visual board layout).
          //   WHITE lookup: pst_x_mg[sq ^ 56]  (mirror = flip rank)
          //   BLACK lookup: pst_x_mg[sq]       (no mirror)
          // Previously constexpr in eval.cpp; moved here so tuning workflows
          // can modify them via eval_params infrastructure.
          // -----------------------------------------------------------------------

          // clang-format off

    // --- Middlegame PSTs ---
    // lambda=0.02, builds 49+50+51+52, 20264 positions. MAE 85.6 cp.

    int pst_pawn_mg[64] = {
         0,   0,   0,   0,   0,   0,   0,   0,  // rank 8
         0,  -1,   6,   2,   1,   0,  -2,   7,  // rank 7
        -1,   5,  -2,   4,   4,   1,   8,   7,  // rank 6
         1,   1, -13,   0,  -2,  -9,  -6, -10,  // rank 5
         2,   0, -16,   3,   8,   1,   8,  -1,  // rank 4
        -11,  16, -37,   9, -12,  -4,  14,  19,  // rank 3
        -4,  11,  -8,  -6, -27,  -3,  34,   3,  // rank 2
         0,   0,   0,   0,   0,   0,   0,   0  // rank 1
    };

    int pst_knight_mg[64] = {
         1,   0,   1,   2,   2,   1,   1,  -3,  // rank 8
         0,   2,   1,   0,   4,   1,  -2,   0,  // rank 7
         0,   5,  -6,   5,   1,   2,   3,   2,  // rank 6
         3,   1,   2,  -1,   1,  11,   4,   4,  // rank 5
         5,   2,  -5,   2,   0,  -1,  -1,   1,  // rank 4
        -7,  -1,   5,  -2,   1,   1,  -1,  -1,  // rank 3
        -2,  -8,   0,  -6,  -7,   2,   1,   4,  // rank 2
         3,  -1,  -5,  -4,  -4,  -4, -20,   2  // rank 1
    };

    int pst_bishop_mg[64] = {
         2,   4,   0,   5,   4,   4,   5,   4,  // rank 8
         4,   1,  -1,  -2,   1,   3,   2,   5,  // rank 7
        -2,  -1,  -4,  -1,  -2, -21,   6,   1,  // rank 6
         1,   5,  -1,  -3,  10,  -1,  -1,   6,  // rank 5
        -4,  -3,  -4,  -7,  -4,   4,   4,   9,  // rank 4
        -10,  -5,   1,  -8,   2,  -7,   7,   3,  // rank 3
         4,  -5,  -4,  -5,  -5,   4,  19,  -2,  // rank 2
         1,   5, -16,  -1,   4, -22,   5,   5  // rank 1
    };

    int pst_rook_mg[64] = {
         3,   1,  -5,  -1,  -5,  -2,   7,   2,  // rank 8
         6,   1,  12,  -2,   2,   0,  13,  -1,  // rank 7
         3,  -1,   4,   0,   2,   0,   6,   1,  // rank 6
         1,   2,   4,  -1,   5,   3,   6,   3,  // rank 5
         0,   4,   1,  -2,   2,   3,   6,   0,  // rank 4
        -4,  -1,   2,  -6,   3,  -4,   1,   2,  // rank 3
        -7,  -8,   3,  -6,   2,  -7, -11,  -3,  // rank 2
        -11,  -4,  -1,  -1,  11,  -4,  -6, -21  // rank 1
    };

    int pst_queen_mg[64] = {
        -1,  -2,  -1, -12,   5,  -1,  -2,  -1,  // rank 8
        -1,   1,   1,  -6,  -9,   1,   1,   3,  // rank 7
        -6,  -8,   6,  -2,  -3, -10,   6,  19,  // rank 6
        -2,   2,  -1,  -1,  -5,   3,  -2,   5,  // rank 5
         1,   3,   0,  -2,  -8,   3,   5,   3,  // rank 4
        -1,  -4,   0,   4,   1,   1,   5,  -2,  // rank 3
         8,   2,  -1,  -2,   1,   4,  -1,   2,  // rank 2
         3,   2,   2,  -7,  -4,  -3,   3,   2  // rank 1
    };

    int pst_king_mg[64] = {
         2,   3,   1,   1,   1,   2,   2,   1,  // rank 8
         0,   1,   4,   3,   3,   2,   3,   1,  // rank 7
         0,   5,   5,   1,   3,   2,   1,   2,  // rank 6
         2,  -6,  -3,   1,   4,   3,   5,  -1,  // rank 5
         2,  -1,   0,   3,   1,   3,   0,   1,  // rank 4
        -3,  -4,   3,   0,  -8,  -2,  -7,  -5,  // rank 3
        -3,  -8,   0,  -7,  -3,   0,   1,  -9,  // rank 2
         4,   0,   5,  -3, -16,  -7,   3,   5  // rank 1
    };

    // --- Endgame PSTs ---

    int pst_pawn_eg[64] = {
         0,   0,   0,   0,   0,   0,   0,   0,  // rank 8
        15,   6,  10,  18,  12,   7,  12,  32,  // rank 7
        20,  30, -12,  11,  14, -11,  14,  12,  // rank 6
         7,   2,   2,  10, -16,  20, -21, -12,  // rank 5
        -13,  -1, -13, -15,   0,   6,   7, -23,  // rank 4
        13,  -2, -28,  -9, -20, -23,  20, -19,  // rank 3
        -23, -23,  -3,  -8, -26,  32,   2, -12,  // rank 2
         0,   0,   0,   0,   0,   0,   0,   0  // rank 1
    };

    int pst_knight_eg[64] = {
         2,   2,   4,   9,   5,   3,   1,   3,  // rank 8
        -1,   8,   2,   0,  12,   2,  -5,   0,  // rank 7
        -1,   7,  -8,   8,  -2,   5,   3,  -1,  // rank 6
         2,   5,   2,   5, -12,   7,   6,   3,  // rank 5
         4,  -5,   0,   4,   4,   9, -10,  -2,  // rank 4
        -8,   2,  -8,   1,  -7,  -9,   0,   1,  // rank 3
         0,  -9,  -5,  -8,  -3,   3,   9,   2,  // rank 2
         3, -17,  -5,  -4,  -3,  -4, -10,   2  // rank 1
    };

    int pst_bishop_eg[64] = {
         2,   3,   2,   5,   2,   6,   3,   1,  // rank 8
         1,   2,  -6,  -1,   4,  -2,   1,   3,  // rank 7
         1,  -4,   1,  -7,   4, -11,  14,  -2,  // rank 6
        -1,   6,   1,   4,  -4,  -1,  -6,   2,  // rank 5
         0,  -2,   7,  -3,   3,  -3,   4,  -4,  // rank 4
        -10,   0,  -9,   4,   4,  -5,   2,   2,  // rank 3
        -2,   0,   4,  -6,  -3,  -9,   7,   5,  // rank 2
        -3,   7,  -9,  -4,  -6,  -5,   5,   6  // rank 1
    };

    int pst_rook_eg[64] = {
         9,   3,  -1,   5,   9,   3,   7,  -2,  // rank 8
         5,   0,  14,  -3,   7,  -2,  11,  -5,  // rank 7
        10,   1,   8,   1,   7,   6,   1,   0,  // rank 6
        -3,   5,   3,  -3,  14,   3,   2,   0,  // rank 5
        -3,   3,   2,  -6,   9,  -1,   5,  -2,  // rank 4
        -8,   4,  -1,  -7,   0,  -2,  -6,  -1,  // rank 3
        -6,  -6,   0,  -4,  -5,  -3, -12,  -5,  // rank 2
        -12,   0, -11,  -6,  -1,  -8,  -5, -14  // rank 1
    };

    int pst_queen_eg[64] = {
        -4,  -6,  -3,  -6,   6,  -2,  -5,  -3,  // rank 8
        -4,   2,   3,  -3,   0,   1,  -1,   2,  // rank 7
        -3,  -5,   7,  -1,  -1, -11,   5,  10,  // rank 6
         1,   1,   1,   3,  -2,   4,  -1,   5,  // rank 5
         0,   1,  -2,  -2,  -4,   6,   6,   4,  // rank 4
         2, -16,  -2,   0,   2,   5,   5,   1,  // rank 3
         3,  -2,  -3,   1,  -1,   3,  -1,   3,  // rank 2
         1,   2,   2,  -4,   0,  -2,   3,   2  // rank 1
    };

    int pst_king_eg[64] = {
         2,   8,   2,   3,   2,   4,  14,   4,  // rank 8
        -4,  -1,   9,  10,  11,  19,  11,   6,  // rank 7
         1,  14,  13,   0,  15,  18,   9,   7,  // rank 6
         5, -10, -11,   7,  14,  23,  16,  -5,  // rank 5
         2,  -5,  -2,   2,   3,   6,   5,  -3,  // rank 4
        -12, -10,  -6,  -7,   3, -13,  -8, -16,  // rank 3
        -1, -28, -14, -16, -19, -18,  -6, -14,  // rank 2
        15,   0,  -8, -10, -13, -17,  -4,  -6  // rank 1
    };

          // clang-format on

          // -----------------------------------------------------------------------
          // Pawn structure
          // -----------------------------------------------------------------------
          int doubled_pawn_penalty = -15;  // Per extra pawn on the same file
          int isolated_pawn_penalty = -20; // Pawn with no friendly pawn on adjacent files
          // Passed-pawn rank bonus (index = absolute rank 0..7; 0 and 7 are unused)
          // Ranks 4-6 partially restored from overbroad reduction in earlier build.
          // Attribution sweep (build 35, 3504 pos) showed pawn_structure corr=-0.485
          // in MG, indicating we undervalue good pawn structure vs Stockfish.
          int passed_pawn_bonus[8] = {0, 12, 17, 25, 38, 55, 80, 0};
          // Separate EG passed-pawn rank bonus (replaces the old "*3/2" heuristic)
          int passed_pawn_eg_bonus[8] = {0, 15, 22, 38, 68, 140, 260, 0};
          // Extra bonus for a protected passer (supported by own pawn)
          int protected_passer_mg = 10;
          int protected_passer_eg = 20;
          // Partial credit for candidate passers (one enemy blocker, can push through)
          int candidate_passer_mg = 5;
          int candidate_passer_eg = 10;

          // -----------------------------------------------------------------------
          // Bishop
          // -----------------------------------------------------------------------
          int bishop_pair_bonus = 52; // b50 OLS 1.747 (was 30; x1.747)

          // -----------------------------------------------------------------------
          // Rook
          // -----------------------------------------------------------------------
          int rook_open_file_bonus = 18;  // MG: rook on fully open file  (reduced 28%: corr=+0.448 in transition)
          int rook_semi_open_bonus = 10;  // MG: rook on semi-open file   (reduced 28%: same signal)
          int rook_open_file_eg = 15;     // EG: rook on fully open file  (unchanged: corr=-0.008 in EG)
          int rook_semi_open_eg = 8;      // EG: rook on semi-open file   (unchanged)
          int rook_seventh_mg = 18;       // b50 OLS -0.611 (reverted to b49)
          int rook_seventh_eg = 30;       // b50 OLS -0.611 (reverted to b49)
          int connected_rooks_bonus = 10; // MG: two rooks that see each other

          // -----------------------------------------------------------------------
          // Outposts — bonus for a piece on a square not attackable by enemy pawns,
          //            optionally supported by a friendly pawn
          // -----------------------------------------------------------------------
          int knight_outpost_supported_mg = 48; // b70 OLS +1.825 (was 12; x4)
          int knight_outpost_supported_eg = 28; // b70 OLS +1.825 (was 7; x4)
          int knight_outpost_mg = 20;           // b70 OLS +1.825 (was 5; x4)   // Unsupported
          int knight_outpost_eg = 8;            // b70 OLS +1.825 (was 2; x4)
          int bishop_outpost_supported_mg = 28; // b70 OLS +1.825 (was 7; x4)
          int bishop_outpost_supported_eg = 16; // b70 OLS +1.825 (was 4; x4)
          int bishop_outpost_mg = 8;            // b70 OLS +1.825 (was 2; x4)   // Unsupported
          int bishop_outpost_eg = 4;            // b70 OLS +1.825 (was 0; x4)

          // -----------------------------------------------------------------------
          // Mobility — bonus per reachable square (non-own-piece targets)
          // CPW-calibrated baseline values.  Previously zeroed under the assumption
          // that mobility double-counted space; attribution showed the mobility
          // term contributes exactly 0 — a ~50-80 Elo gap.
          // -----------------------------------------------------------------------
          int knight_mobility_mg = 8; // b70 OLS +1.595/MG+2.19 (was 4; x2 MG only)
          int bishop_mobility_mg = 8; // b70 OLS +1.595/MG+2.19 (was 4; x2 MG only)
          int rook_mobility_mg = 4;   // b70 OLS +1.595/MG+2.19 (was 2; x2 MG only)

          // -----------------------------------------------------------------------
          // King safety
          // -----------------------------------------------------------------------
          int king_attacker_weight_knight = 2; // was 1 — b88 king-safety audit +125cp gap
          int king_attacker_weight_bishop = 2; // was 1
          int king_attacker_weight_rook = 3;   // was 2
          int king_attacker_weight_queen = 5;  // was 4
          int pawn_shield_bonus = 14;          // Per pawn in shield         (doubled from 7: build-43 king_safety OLS +2.0×)
          int king_open_file_penalty = 24;     // Per open/semi-open file near king  (doubled from 12: build-43)
          int king_open_file_full_extra = 16;  // Extra if file is fully open (doubled from 8: build-43)

          // -----------------------------------------------------------------------
          // Tempo / initiative
          // -----------------------------------------------------------------------
          int tempo_bonus = 22; // b59 OLS +6.4 (was 11; doubled)

          // -----------------------------------------------------------------------
          // Castling urgency & castled bonus
          // -----------------------------------------------------------------------
          int castling_urgency_penalty = 0; // b52 OLS -0.906 (zeroed)
          int castled_bonus_mg = 15;        // b88: restored small value (was 0 since b52)

          // -----------------------------------------------------------------------
          // Mopup evaluation (winning endgame: shepherd losing king to corner)
          // -----------------------------------------------------------------------
          int mopup_corner_weight = 8;        // b70 OLS -0.785 (was 17; halved)
          int mopup_distance_weight = 3;      // b70 OLS -0.785 (was 7; halved)
          int mopup_material_threshold = 150; // b70 threshold lowered (was 450; fires too rarely)

          // -----------------------------------------------------------------------
          // Pinned pieces — penalty for pieces absolutely pinned to their king
          // -----------------------------------------------------------------------
          int pinned_piece_penalty_mg = 0; // b49 OLS -0.752 (was -18; zeroed)
          int pinned_piece_penalty_eg = 0; // b49 OLS -0.752 (was -10; zeroed)

          // -----------------------------------------------------------------------
          // Backward pawn — no friendly support behind, stop square pawn-attacked
          // -----------------------------------------------------------------------
          int backward_pawn_penalty_mg = -15; // strengthened: attribution corr=-0.485 in MG
          int backward_pawn_penalty_eg = -8;

          // -----------------------------------------------------------------------
          // Connected / defended pawns — pawn protected by another pawn
          // -----------------------------------------------------------------------
          int connected_pawn_bonus_mg = 10; // strengthened: attribution corr=-0.485 in MG
          int connected_pawn_bonus_eg = 5;

          // -----------------------------------------------------------------------
          // Bad bishop — own pawns on same color complex as the bishop
          // -----------------------------------------------------------------------
          int bad_bishop_per_pawn_mg = -4; // b70 OLS 0.412 (was -9; halved — overcounts mobile pawns)   // b51 OLS 0.761 (was -12; x0.761�-9)
          int bad_bishop_per_pawn_eg = -4; // b70 OLS 0.412 (was -8; halved)   // b51 OLS 0.761 (was -11; x0.761�-8)

          // -----------------------------------------------------------------------
          // Space — safe central squares behind own pawn chain
          // -----------------------------------------------------------------------
          int space_bonus_mg = 7; // b50 OLS 0.443 (was 17; 17�0.443�7)

          // -----------------------------------------------------------------------
          // Threats — bonus for attacking more valuable enemy pieces
          // -----------------------------------------------------------------------
          int threat_by_pawn_mg = 150; // b59 OLS +1.55 (was 100; x1.5)
          int threat_by_minor_mg = 75; // b59 OLS +1.55 (was 50; x1.5)
          int threat_by_rook_mg = 38;  // b59 OLS +1.55 (was 25; x1.5)
          int threat_by_pawn_eg = 60;  // EG threats
          int threat_by_minor_eg = 38; // EG threats
          int threat_by_rook_eg = 20;  // EG threats

          // -----------------------------------------------------------------------
          // Extended mobility — EG components + queen mobility
          // -----------------------------------------------------------------------
          int knight_mobility_eg = 4; // b59 OLS +1.43 (was 3)
          int bishop_mobility_eg = 4; // b59 OLS +1.43 (was 3)
          int rook_mobility_eg = 2;   // b59 OLS +1.43 (was 1)
          int queen_mobility_mg = 1;
          int queen_mobility_eg = 1;

          // -----------------------------------------------------------------------
          // Bishop pair — endgame component (MG already exists above)
          // -----------------------------------------------------------------------
          int bishop_pair_eg_bonus = 79; // b50 OLS 1.747 (was 45; x1.747)

          // -----------------------------------------------------------------------
          // Rook behind passed pawn
          // -----------------------------------------------------------------------
          int rook_behind_passer_mg = 0; // b49 OLS -1.059 (was 15; zeroed)
          int rook_behind_passer_eg = 0; // b49 OLS -1.059 (was 25; zeroed)

          // -----------------------------------------------------------------------
          // Enemy rook on the same file as our passed pawn (can intercept or blockade)
          // Applied when any enemy rook sits on the passer's file — the passer bonus
          // is reduced because the rook can slide to the promotion square cheaply.
          // -----------------------------------------------------------------------
          int enemy_rook_on_passer_file_mg = -20;
          int enemy_rook_on_passer_file_eg = -60;

          // -----------------------------------------------------------------------
          // King proximity to passed pawns (endgame)
          // -----------------------------------------------------------------------
          // Halved again from 10/6: build-39 Corr=0.686 with ±500cp filter, still dominant overweight term.
          int king_passer_support_eg = 5; // b88: restored (was 0 since b59 bug fix)
          int king_passer_threat_eg = 3;  // b88: restored (was 0 since b59)
          int king_passer_support_mg = 2; // b88: restored (was 0 since b59)
          int king_passer_threat_mg = 1;  // b88: restored (was 0 since b59)

          // -----------------------------------------------------------------------
          // Wrong-colour bishop draw: when a side has only one bishop and all its
          // passed pawn(s) promote on the opposite colour from that bishop, the
          // endgame is drawn.  We apply a penalty per such passer to counteract
          // the normal passer bonus already baked in.
          // -----------------------------------------------------------------------
          int wrong_bishop_passer_penalty_eg = -120; // Negative offset per qualifying passer

          // -----------------------------------------------------------------------
          // Piece-blockaded passed pawn (EG): enemy non-pawn piece sitting on the
          // stop square permanently prevents the passer from advancing.  Cancel
          // the base passer bonus entirely and apply an extra penalty — the frozen
          // pawn restricts our own king's endgame activity.
          // Audit (b89): passer_block_01 d1=-113 vs SF=-596 (Δ483cp).
          // -----------------------------------------------------------------------
          int passer_blockade_piece_eg = 60; // extra penalty on top of cancelling base bonus

          // -----------------------------------------------------------------------
          // Outside passed pawn (EG): passer is on the opposite wing from all enemy pawns.
          // In K+P endings this is near-decisive — the outside passer diverts the
          // opposing king, allowing our king to scoop the other-wing pawns.
          // Audit (b88): d1=135 vs SF=1055 (CRITICAL, Δ920cp).
          // -----------------------------------------------------------------------
          int outside_passer_eg = 200; // per qualifying outside passer

          // -----------------------------------------------------------------------
          // Unstoppable passer — Rule of the Square (EG):
          // Defending king is outside the pawn's square and cannot enter it even
          // with the move.  Pawn promotes for free.
          // Audit (b88): pawn_race_chebyshev d1=-399 vs SF=-536 (Δ137cp).
          // Threshold: king_dist > moves_to_promo + 1  (defending king gets benefit of doubt)
          // -----------------------------------------------------------------------
          int unstoppable_passer_eg = 200; // per unstoppable passer
          // -----------------------------------------------------------------------
          // Weak / undefended minor pieces
          // -----------------------------------------------------------------------
          int weak_minor_penalty_mg = 0; // b48 OLS -2.756 (was -15; zeroed)
          int weak_minor_penalty_eg = 0; // b48 OLS -2.756 (was -10; zeroed)

          // -----------------------------------------------------------------------
          // Weak color complex (EG): one side has bishop(s), opponent has NO bishops.
          // The bishop-owner controls all squares of their bishop's color(s) unopposed.
          // Audit (b88): weak_color_complex d1=397 vs SF=534 (minor, Δ137cp).
          // -----------------------------------------------------------------------
          int color_complex_bishop_eg = 60; // flat EG bonus per bishop with no opposing same-color bishop
          int color_complex_pawn_eg = 8;    // additional per enemy pawn sitting on the dominated color

          // -----------------------------------------------------------------------
          // Center pawn advance — extra MG bonus for d/e pawns that have reached
          // rank 4 or 5, supplementing the PST to better compete with piece sorties.
          // -----------------------------------------------------------------------
          int center_pawn_advance_mg = 0; // Per qualifying d/e pawn (disabled: too disruptive in search)

          // -----------------------------------------------------------------------
          // Queen early development penalty — discourage bringing the queen out
          // while own minor pieces (knights / bishops) are still on their home squares.
          // Applied per undeveloped minor when the queen has left its starting square.
          // -----------------------------------------------------------------------
          int queen_early_dev_penalty_mg = 8; // cp per undeveloped minor

          // -----------------------------------------------------------------------
          // Pin-creation bonus — reward for our slider X-raying through an enemy
          // piece to a more valuable enemy piece (or enemy king).
          // This encourages moves like Bg5 that create pins.
          // -----------------------------------------------------------------------
          int pin_creation_bonus_mg = 22; // b70 OLS +1.922 (was 11; x2)   // b51 OLS 1.816 (was 6; 6x1.816�11 | b50 OLS 0.246 (was 26; 26�0.246�6)
          int pin_creation_bonus_eg = 10; // b70 OLS +1.922 (was 5; x2)   // b51 OLS 1.816 (was 3; 3x1.816�5 | b50 OLS 0.246 (was 13; 13�0.246�3)

          // -----------------------------------------------------------------------
          // Endgame draw scaling — reduce eval in drawish endgames.
          // Scale factor is out of 256 (256 = no change, 128 = halved).
          // -----------------------------------------------------------------------
          int ocb_scale = 128;             // Opposite-color bishops (50%)
          int ocb_no_pawns_scale = 96;     // OCB + no pawns either side (37%)
          int pawn_scarcity_base = 128;    // Base scale when winning side has 0 pawns
          int pawn_scarcity_per_pawn = 16; // Added per pawn of winning side
     };

     // Global singleton — use everywhere in eval.cpp
     extern EvalParams g_eval_params;

     // Accessors
     const EvalParams &get_eval_params();
     void set_eval_params(const EvalParams &p);

     // Set a single parameter by its UCI option name (returns false if unknown)
     bool set_eval_param_by_name(const std::string &name, int value);

     // Build a UCI option-string block advertising all eval parameters.
     // Call from cmd_uci() to expose params to GUI / tuning tools.
     std::string eval_options_uci_string();

     // Optional: load parameters from a simple "name=value" text file.
     // Returns false and leaves params unchanged if the file cannot be read.
     bool load_eval_params_from_file(const std::string &path);

} // namespace Chess
