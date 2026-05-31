#pragma once
// ============================================================================
// NNUE Eval — public evaluation interface for the search
//
// Provides a single function that the search can call:
//   int nnue_evaluate(const Board& board)
//
// If the NNUE network has been loaded, this performs incremental accumulator
// evaluation.  Otherwise it falls back to the classical HCE.
//
// The accumulator is stored in the Board's StateInfo, so do_move / undo_move
// maintain it automatically (via hooks in board.cpp).
// ============================================================================

#include "../types.h"
#include <string>

namespace Chess {
class Board;
}

namespace Chess::NNUE {

struct ProfileSnapshot {
	bool cycle_counters_enabled = false;
	bool loaded = false;
	bool runtime_enabled = true;
	uint64_t eval_calls = 0;
	uint64_t fallback_calls = 0;
	uint64_t refresh_calls = 0;
	uint64_t forward_calls = 0;
	uint64_t incremental_updates = 0;
	uint64_t capture_updates = 0;
	uint64_t full_updates = 0;
	uint64_t null_updates = 0;
	uint64_t cycles_refresh = 0;
	uint64_t cycles_forward = 0;
	uint64_t cycles_update = 0;
	uint64_t cycles_full_update = 0;
	uint64_t cycles_null_update = 0;
};

// Initialise the NNUE subsystem: load weights from `path`.
// Returns true on success.  If loading fails the engine falls back to HCE.
bool nnue_init(const std::string& path);

// Auto-discover nn.bin next to the engine binary, then in parent dirs.
// Called once at startup before the UCI loop.  `argv0` is argv[0] from main.
// Returns true if NNUE was loaded.
bool nnue_auto_discover(const char* argv0);

// Runtime enable/disable toggle.  When disabled (UseNNUE=false) every call
// to nnue_evaluate() falls through to the classical HCE regardless of whether
// a network is loaded.  Equivalent to the compile-time DISABLE_NNUE flag but
// settable at runtime via `setoption name UseNNUE value false`.
void nnue_set_enabled(bool enabled);
bool nnue_is_enabled();

// Control informational stdout messages emitted by the NNUE loader/runtime.
void nnue_set_info_enabled(bool enabled);
bool nnue_info_enabled();
void nnue_set_profile_enabled(bool enabled);
bool nnue_profile_enabled();

// Main evaluation entry point — replaces evaluate() in the search.
// Returns a score in centipawns from the side-to-move's perspective.
int nnue_evaluate(const Board& board);

bool nnue_is_loaded();

// Diagnostic: returns refresh ratio as a printable string
// Format: "evals=NNN refreshes=NNN pct=N%"
std::string nnue_refresh_stats();

// Structured profiling support for engine-side benchmarks and hotspot analysis.
void nnue_profile_reset();
ProfileSnapshot nnue_profile_snapshot();
void nnue_profile_note_do_move_update(bool full_refresh, bool capture, uint64_t cycles = 0);
void nnue_profile_note_null_update(uint64_t cycles = 0);
void nnue_profile_note_full_update(uint64_t cycles = 0);

} // namespace Chess::NNUE
