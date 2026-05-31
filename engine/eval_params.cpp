#include "eval_params.h"

#include <fstream>
#include <functional>
#include <sstream>
#include <string>
#include <unordered_map>

namespace Chess {

// ============================================================================
// Global singleton — zero-initialised then overwritten by default constructors
// ============================================================================
EvalParams g_eval_params;

const EvalParams& get_eval_params() { return g_eval_params; }
void              set_eval_params(const EvalParams& p) { g_eval_params = p; }

// ============================================================================
// Dispatch table: UCI option name → mutating lambda on EvalParams
// Names use the "Eval" prefix so they are clearly grouped in any GUI.
// ============================================================================

using Setter = std::function<void(EvalParams&, int)>;

// clang-format off
static const std::unordered_map<std::string, Setter>& param_setters() {
    static const std::unordered_map<std::string, Setter> table = {
        // Pawn structure
        {"EvalDoubledPawnPenalty",          [](EvalParams& p, int v){ p.doubled_pawn_penalty   = v; }},
        {"EvalIsolatedPawnPenalty",         [](EvalParams& p, int v){ p.isolated_pawn_penalty  = v; }},
        {"EvalPassedPawnBonusR1",           [](EvalParams& p, int v){ p.passed_pawn_bonus[1]   = v; }},
        {"EvalPassedPawnBonusR2",           [](EvalParams& p, int v){ p.passed_pawn_bonus[2]   = v; }},
        {"EvalPassedPawnBonusR3",           [](EvalParams& p, int v){ p.passed_pawn_bonus[3]   = v; }},
        {"EvalPassedPawnBonusR4",           [](EvalParams& p, int v){ p.passed_pawn_bonus[4]   = v; }},
        {"EvalPassedPawnBonusR5",           [](EvalParams& p, int v){ p.passed_pawn_bonus[5]   = v; }},
        {"EvalPassedPawnBonusR6",           [](EvalParams& p, int v){ p.passed_pawn_bonus[6]   = v; }},
        {"EvalPassedPawnEGR1",              [](EvalParams& p, int v){ p.passed_pawn_eg_bonus[1] = v; }},
        {"EvalPassedPawnEGR2",              [](EvalParams& p, int v){ p.passed_pawn_eg_bonus[2] = v; }},
        {"EvalPassedPawnEGR3",              [](EvalParams& p, int v){ p.passed_pawn_eg_bonus[3] = v; }},
        {"EvalPassedPawnEGR4",              [](EvalParams& p, int v){ p.passed_pawn_eg_bonus[4] = v; }},
        {"EvalPassedPawnEGR5",              [](EvalParams& p, int v){ p.passed_pawn_eg_bonus[5] = v; }},
        {"EvalPassedPawnEGR6",              [](EvalParams& p, int v){ p.passed_pawn_eg_bonus[6] = v; }},
        {"EvalProtectedPasserMG",           [](EvalParams& p, int v){ p.protected_passer_mg     = v; }},
        {"EvalProtectedPasserEG",           [](EvalParams& p, int v){ p.protected_passer_eg     = v; }},
        {"EvalCandidatePasserMG",           [](EvalParams& p, int v){ p.candidate_passer_mg     = v; }},
        {"EvalCandidatePasserEG",           [](EvalParams& p, int v){ p.candidate_passer_eg     = v; }},
        // Bishop
        {"EvalBishopPairBonus",             [](EvalParams& p, int v){ p.bishop_pair_bonus               = v; }},
        // Rook
        {"EvalRookOpenFileBonus",           [](EvalParams& p, int v){ p.rook_open_file_bonus            = v; }},
        {"EvalRookSemiOpenBonus",           [](EvalParams& p, int v){ p.rook_semi_open_bonus            = v; }},
        {"EvalRookOpenFileEG",             [](EvalParams& p, int v){ p.rook_open_file_eg               = v; }},
        {"EvalRookSemiOpenEG",             [](EvalParams& p, int v){ p.rook_semi_open_eg               = v; }},
        {"EvalRookSeventhMG",               [](EvalParams& p, int v){ p.rook_seventh_mg                 = v; }},
        {"EvalRookSeventhEG",               [](EvalParams& p, int v){ p.rook_seventh_eg                 = v; }},
        {"EvalConnectedRooksBonus",         [](EvalParams& p, int v){ p.connected_rooks_bonus           = v; }},
        // Outposts
        {"EvalKnightOutpostSupportedMG",    [](EvalParams& p, int v){ p.knight_outpost_supported_mg     = v; }},
        {"EvalKnightOutpostSupportedEG",    [](EvalParams& p, int v){ p.knight_outpost_supported_eg     = v; }},
        {"EvalKnightOutpostMG",             [](EvalParams& p, int v){ p.knight_outpost_mg               = v; }},
        {"EvalKnightOutpostEG",             [](EvalParams& p, int v){ p.knight_outpost_eg               = v; }},
        {"EvalBishopOutpostSupportedMG",    [](EvalParams& p, int v){ p.bishop_outpost_supported_mg     = v; }},
        {"EvalBishopOutpostSupportedEG",    [](EvalParams& p, int v){ p.bishop_outpost_supported_eg     = v; }},
        {"EvalBishopOutpostMG",             [](EvalParams& p, int v){ p.bishop_outpost_mg               = v; }},
        {"EvalBishopOutpostEG",             [](EvalParams& p, int v){ p.bishop_outpost_eg               = v; }},
        // Mobility
        {"EvalKnightMobilityMG",            [](EvalParams& p, int v){ p.knight_mobility_mg              = v; }},
        {"EvalBishopMobilityMG",            [](EvalParams& p, int v){ p.bishop_mobility_mg              = v; }},
        {"EvalRookMobilityMG",              [](EvalParams& p, int v){ p.rook_mobility_mg                = v; }},
        // King safety
        {"EvalKingAttackerWeightKnight",    [](EvalParams& p, int v){ p.king_attacker_weight_knight     = v; }},
        {"EvalKingAttackerWeightBishop",    [](EvalParams& p, int v){ p.king_attacker_weight_bishop     = v; }},
        {"EvalKingAttackerWeightRook",      [](EvalParams& p, int v){ p.king_attacker_weight_rook       = v; }},
        {"EvalKingAttackerWeightQueen",     [](EvalParams& p, int v){ p.king_attacker_weight_queen      = v; }},
        {"EvalPawnShieldBonus",             [](EvalParams& p, int v){ p.pawn_shield_bonus               = v; }},
        {"EvalKingOpenFilePenalty",         [](EvalParams& p, int v){ p.king_open_file_penalty          = v; }},
        {"EvalKingOpenFileFullExtra",       [](EvalParams& p, int v){ p.king_open_file_full_extra       = v; }},
        // Tempo / castling
        {"EvalTempoBonus",                  [](EvalParams& p, int v){ p.tempo_bonus                     = v; }},
        {"EvalCastlingUrgencyPenalty",      [](EvalParams& p, int v){ p.castling_urgency_penalty        = v; }},
        {"EvalCastledBonusMG",             [](EvalParams& p, int v){ p.castled_bonus_mg                = v; }},
        // Mopup
        {"EvalMopupCornerWeight",           [](EvalParams& p, int v){ p.mopup_corner_weight             = v; }},
        {"EvalMopupDistanceWeight",         [](EvalParams& p, int v){ p.mopup_distance_weight           = v; }},
        {"EvalMopupMaterialThreshold",      [](EvalParams& p, int v){ p.mopup_material_threshold        = v; }},
        // Pinned pieces
        {"EvalPinnedPiecePenaltyMG",        [](EvalParams& p, int v){ p.pinned_piece_penalty_mg          = v; }},
        {"EvalPinnedPiecePenaltyEG",        [](EvalParams& p, int v){ p.pinned_piece_penalty_eg          = v; }},
        // Backward pawn
        {"EvalBackwardPawnPenaltyMG",       [](EvalParams& p, int v){ p.backward_pawn_penalty_mg         = v; }},
        {"EvalBackwardPawnPenaltyEG",       [](EvalParams& p, int v){ p.backward_pawn_penalty_eg         = v; }},
        // Connected pawns
        {"EvalConnectedPawnBonusMG",        [](EvalParams& p, int v){ p.connected_pawn_bonus_mg          = v; }},
        {"EvalConnectedPawnBonusEG",        [](EvalParams& p, int v){ p.connected_pawn_bonus_eg          = v; }},
        // Bad bishop
        {"EvalBadBishopPerPawnMG",          [](EvalParams& p, int v){ p.bad_bishop_per_pawn_mg           = v; }},
        {"EvalBadBishopPerPawnEG",          [](EvalParams& p, int v){ p.bad_bishop_per_pawn_eg           = v; }},
        // Space
        {"EvalSpaceBonusMG",               [](EvalParams& p, int v){ p.space_bonus_mg                   = v; }},
        // Threats
        {"EvalThreatByPawnMG",             [](EvalParams& p, int v){ p.threat_by_pawn_mg                = v; }},
        {"EvalThreatByMinorMG",            [](EvalParams& p, int v){ p.threat_by_minor_mg               = v; }},
        {"EvalThreatByRookMG",             [](EvalParams& p, int v){ p.threat_by_rook_mg                = v; }},
        {"EvalThreatByPawnEG",             [](EvalParams& p, int v){ p.threat_by_pawn_eg                = v; }},
        {"EvalThreatByMinorEG",            [](EvalParams& p, int v){ p.threat_by_minor_eg               = v; }},
        {"EvalThreatByRookEG",             [](EvalParams& p, int v){ p.threat_by_rook_eg                = v; }},
        // Extended mobility
        {"EvalKnightMobilityEG",           [](EvalParams& p, int v){ p.knight_mobility_eg               = v; }},
        {"EvalBishopMobilityEG",           [](EvalParams& p, int v){ p.bishop_mobility_eg               = v; }},
        {"EvalRookMobilityEG",             [](EvalParams& p, int v){ p.rook_mobility_eg                 = v; }},
        {"EvalQueenMobilityMG",            [](EvalParams& p, int v){ p.queen_mobility_mg                = v; }},
        {"EvalQueenMobilityEG",            [](EvalParams& p, int v){ p.queen_mobility_eg                = v; }},
        // Bishop pair EG
        {"EvalBishopPairEGBonus",           [](EvalParams& p, int v){ p.bishop_pair_eg_bonus             = v; }},
        // Rook behind passer
        {"EvalRookBehindPasserMG",          [](EvalParams& p, int v){ p.rook_behind_passer_mg            = v; }},
        {"EvalRookBehindPasserEG",          [](EvalParams& p, int v){ p.rook_behind_passer_eg            = v; }},
        // Enemy rook on passer file
        {"EvalEnemyRookOnPasserFileMG",     [](EvalParams& p, int v){ p.enemy_rook_on_passer_file_mg    = v; }},
        {"EvalEnemyRookOnPasserFileEG",     [](EvalParams& p, int v){ p.enemy_rook_on_passer_file_eg    = v; }},
        // King-passer distance
        {"EvalKingPasserSupportEG",         [](EvalParams& p, int v){ p.king_passer_support_eg           = v; }},
        {"EvalKingPasserThreatEG",          [](EvalParams& p, int v){ p.king_passer_threat_eg            = v; }},
        // Weak minor
        {"EvalWeakMinorPenaltyMG",          [](EvalParams& p, int v){ p.weak_minor_penalty_mg            = v; }},
        {"EvalWeakMinorPenaltyEG",          [](EvalParams& p, int v){ p.weak_minor_penalty_eg            = v; }},
        // Pin creation
        {"EvalPinCreationBonusMG",          [](EvalParams& p, int v){ p.pin_creation_bonus_mg            = v; }},
        {"EvalPinCreationBonusEG",          [](EvalParams& p, int v){ p.pin_creation_bonus_eg            = v; }},
        // Center pawn advance
        {"EvalCenterPawnAdvanceMG",         [](EvalParams& p, int v){ p.center_pawn_advance_mg           = v; }},
        // Queen early development penalty
        {"EvalQueenEarlyDevPenaltyMG",      [](EvalParams& p, int v){ p.queen_early_dev_penalty_mg       = v; }},
    };
    return table;
}
// clang-format on

bool set_eval_param_by_name(const std::string& name, int value) {
    auto& table = param_setters();
    auto it = table.find(name);
    if (it == table.end()) return false;
    it->second(g_eval_params, value);
    return true;
}

// ============================================================================
// UCI option string builder
// Emits one "option name ... type spin default ... min ... max ..." line
// for each parameter. The defaults are read from a freshly default-constructed
// EvalParams so the display always reflects the compile-time defaults.
// ============================================================================

static std::string spin_line(const char* name, int def_val, int min_v, int max_v) {
    std::ostringstream oss;
    oss << "option name " << name
        << " type spin default " << def_val
        << " min " << min_v
        << " max " << max_v;
    return oss.str();
}

std::string eval_options_uci_string() {
    const EvalParams DEF; // default-constructed → compile-time defaults
    std::ostringstream out;

    auto line = [&](const char* name, int val, int lo, int hi) {
        out << spin_line(name, val, lo, hi) << "\n";
    };

    // Pawn
    line("EvalDoubledPawnPenalty",       DEF.doubled_pawn_penalty,       -150,  0   );
    line("EvalIsolatedPawnPenalty",      DEF.isolated_pawn_penalty,      -150,  0   );
    line("EvalPassedPawnBonusR1",        DEF.passed_pawn_bonus[1],          0, 200  );
    line("EvalPassedPawnBonusR2",        DEF.passed_pawn_bonus[2],          0, 200  );
    line("EvalPassedPawnBonusR3",        DEF.passed_pawn_bonus[3],          0, 200  );
    line("EvalPassedPawnBonusR4",        DEF.passed_pawn_bonus[4],          0, 200  );
    line("EvalPassedPawnBonusR5",        DEF.passed_pawn_bonus[5],          0, 200  );
    line("EvalPassedPawnBonusR6",        DEF.passed_pawn_bonus[6],          0, 300  );
    line("EvalPassedPawnEGR1",           DEF.passed_pawn_eg_bonus[1],       0, 200  );
    line("EvalPassedPawnEGR2",           DEF.passed_pawn_eg_bonus[2],       0, 200  );
    line("EvalPassedPawnEGR3",           DEF.passed_pawn_eg_bonus[3],       0, 200  );
    line("EvalPassedPawnEGR4",           DEF.passed_pawn_eg_bonus[4],       0, 300  );
    line("EvalPassedPawnEGR5",           DEF.passed_pawn_eg_bonus[5],       0, 400  );
    line("EvalPassedPawnEGR6",           DEF.passed_pawn_eg_bonus[6],       0, 500  );
    line("EvalProtectedPasserMG",        DEF.protected_passer_mg,           0,  50  );
    line("EvalProtectedPasserEG",        DEF.protected_passer_eg,           0,  80  );
    line("EvalCandidatePasserMG",        DEF.candidate_passer_mg,           0,  30  );
    line("EvalCandidatePasserEG",        DEF.candidate_passer_eg,           0,  50  );
    // Bishop
    line("EvalBishopPairBonus",          DEF.bishop_pair_bonus,             0, 100  );
    // Rook
    line("EvalRookOpenFileBonus",        DEF.rook_open_file_bonus,          0, 60   );
    line("EvalRookSemiOpenBonus",        DEF.rook_semi_open_bonus,          0, 40   );
    line("EvalRookOpenFileEG",           DEF.rook_open_file_eg,             0, 40   );
    line("EvalRookSemiOpenEG",           DEF.rook_semi_open_eg,             0, 25   );
    line("EvalRookSeventhMG",            DEF.rook_seventh_mg,               0, 80   );
    line("EvalRookSeventhEG",            DEF.rook_seventh_eg,               0, 100  );
    line("EvalConnectedRooksBonus",      DEF.connected_rooks_bonus,         0, 40   );
    // Outposts
    line("EvalKnightOutpostSupportedMG", DEF.knight_outpost_supported_mg,   0, 80   );
    line("EvalKnightOutpostSupportedEG", DEF.knight_outpost_supported_eg,   0, 60   );
    line("EvalKnightOutpostMG",          DEF.knight_outpost_mg,             0, 40   );
    line("EvalKnightOutpostEG",          DEF.knight_outpost_eg,             0, 30   );
    line("EvalBishopOutpostSupportedMG", DEF.bishop_outpost_supported_mg,   0, 60   );
    line("EvalBishopOutpostSupportedEG", DEF.bishop_outpost_supported_eg,   0, 40   );
    line("EvalBishopOutpostMG",          DEF.bishop_outpost_mg,             0, 30   );
    line("EvalBishopOutpostEG",          DEF.bishop_outpost_eg,             0, 20   );
    // Mobility
    line("EvalKnightMobilityMG",         DEF.knight_mobility_mg,            0, 20   );
    line("EvalBishopMobilityMG",         DEF.bishop_mobility_mg,            0, 20   );
    line("EvalRookMobilityMG",           DEF.rook_mobility_mg,              0, 15   );
    // King safety
    line("EvalKingAttackerWeightKnight", DEF.king_attacker_weight_knight,   0, 10   );
    line("EvalKingAttackerWeightBishop", DEF.king_attacker_weight_bishop,   0, 10   );
    line("EvalKingAttackerWeightRook",   DEF.king_attacker_weight_rook,     0, 15   );
    line("EvalKingAttackerWeightQueen",  DEF.king_attacker_weight_queen,    0, 20   );
    line("EvalPawnShieldBonus",          DEF.pawn_shield_bonus,             0, 40   );
    line("EvalKingOpenFilePenalty",      DEF.king_open_file_penalty,        0, 60   );
    line("EvalKingOpenFileFullExtra",    DEF.king_open_file_full_extra,     0, 40   );
    // Tempo / castling
    line("EvalTempoBonus",               DEF.tempo_bonus,                   0, 40   );
    line("EvalCastlingUrgencyPenalty",   DEF.castling_urgency_penalty,      0, 40   );
    line("EvalCastledBonusMG",           DEF.castled_bonus_mg,              0, 60   );
    // Mopup
    line("EvalMopupCornerWeight",        DEF.mopup_corner_weight,           0, 40   );
    line("EvalMopupDistanceWeight",      DEF.mopup_distance_weight,         0, 20   );
    line("EvalMopupMaterialThreshold",   DEF.mopup_material_threshold,     50, 800  );
    // Pinned pieces
    line("EvalPinnedPiecePenaltyMG",      DEF.pinned_piece_penalty_mg,    -80,   0  );
    line("EvalPinnedPiecePenaltyEG",      DEF.pinned_piece_penalty_eg,    -60,   0  );
    // Backward pawn
    line("EvalBackwardPawnPenaltyMG",     DEF.backward_pawn_penalty_mg,   -60,   0  );
    line("EvalBackwardPawnPenaltyEG",     DEF.backward_pawn_penalty_eg,   -40,   0  );
    // Connected pawns
    line("EvalConnectedPawnBonusMG",      DEF.connected_pawn_bonus_mg,      0,  30  );
    line("EvalConnectedPawnBonusEG",      DEF.connected_pawn_bonus_eg,      0,  20  );
    // Bad bishop
    line("EvalBadBishopPerPawnMG",        DEF.bad_bishop_per_pawn_mg,     -20,   0  );
    line("EvalBadBishopPerPawnEG",        DEF.bad_bishop_per_pawn_eg,     -30,   0  );
    // Space
    line("EvalSpaceBonusMG",             DEF.space_bonus_mg,               0,  15  );
    // Threats
    line("EvalThreatByPawnMG",           DEF.threat_by_pawn_mg,            0, 100  );
    line("EvalThreatByMinorMG",          DEF.threat_by_minor_mg,           0,  60  );
    line("EvalThreatByRookMG",           DEF.threat_by_rook_mg,            0,  40  );
    line("EvalThreatByPawnEG",           DEF.threat_by_pawn_eg,            0,  60  );
    line("EvalThreatByMinorEG",          DEF.threat_by_minor_eg,           0,  40  );
    line("EvalThreatByRookEG",           DEF.threat_by_rook_eg,            0,  30  );
    // Extended mobility
    line("EvalKnightMobilityEG",         DEF.knight_mobility_eg,           0,  15  );
    line("EvalBishopMobilityEG",         DEF.bishop_mobility_eg,           0,  15  );
    line("EvalRookMobilityEG",           DEF.rook_mobility_eg,             0,  10  );
    line("EvalQueenMobilityMG",          DEF.queen_mobility_mg,            0,  10  );
    line("EvalQueenMobilityEG",          DEF.queen_mobility_eg,            0,  10  );
    // Bishop pair EG
    line("EvalBishopPairEGBonus",        DEF.bishop_pair_eg_bonus,         0, 100  );
    // Rook behind passer
    line("EvalRookBehindPasserMG",       DEF.rook_behind_passer_mg,        0,  50  );
    line("EvalRookBehindPasserEG",       DEF.rook_behind_passer_eg,        0,  80  );
    // Enemy rook on passer file
    line("EvalEnemyRookOnPasserFileMG",  DEF.enemy_rook_on_passer_file_mg, -100, 0 );
    line("EvalEnemyRookOnPasserFileEG",  DEF.enemy_rook_on_passer_file_eg, -150, 0 );
    // King-passer distance
    line("EvalKingPasserSupportEG",      DEF.king_passer_support_eg,       0,  20  );
    line("EvalKingPasserThreatEG",       DEF.king_passer_threat_eg,        0,  15  );
    // Weak minor
    line("EvalWeakMinorPenaltyMG",       DEF.weak_minor_penalty_mg,      -60,   0  );
    line("EvalWeakMinorPenaltyEG",       DEF.weak_minor_penalty_eg,      -40,   0  );
    // Pin creation
    line("EvalPinCreationBonusMG",       DEF.pin_creation_bonus_mg,        0,  80  );
    line("EvalPinCreationBonusEG",       DEF.pin_creation_bonus_eg,        0,  50  );
    // Center pawn advance
    line("EvalCenterPawnAdvanceMG",      DEF.center_pawn_advance_mg,       0,  30  );
    // Queen early development penalty
    line("EvalQueenEarlyDevPenaltyMG",   DEF.queen_early_dev_penalty_mg,   0,  40  );

    return out.str();
}

// ============================================================================
// File loader — simple "name=value" format, one per line, # comments allowed
// ============================================================================
bool load_eval_params_from_file(const std::string& path) {
    std::ifstream f(path);
    if (!f.is_open()) return false;

    std::string line;
    while (std::getline(f, line)) {
        // Strip comments
        auto hash_pos = line.find('#');
        if (hash_pos != std::string::npos) line.erase(hash_pos);

        auto eq = line.find('=');
        if (eq == std::string::npos) continue;

        std::string name  = line.substr(0, eq);
        std::string value = line.substr(eq + 1);

        // Trim whitespace
        auto trim = [](std::string& s) {
            size_t start = s.find_first_not_of(" \t\r");
            size_t end   = s.find_last_not_of(" \t\r");
            s = (start == std::string::npos) ? "" : s.substr(start, end - start + 1);
        };
        trim(name);
        trim(value);
        if (name.empty() || value.empty()) continue;

        try {
            set_eval_param_by_name(name, std::stoi(value));
        } catch (...) {}
    }
    return true;
}

} // namespace Chess
