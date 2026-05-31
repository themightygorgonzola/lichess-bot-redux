#pragma once

#include "types.h"
#include <vector>

// ============================================================================
// Pawn Hash Table — caches pawn structure evaluation scores
// Pawn structure changes infrequently and is expensive to evaluate,
// making it ideal for a small dedicated cache.
// ============================================================================

namespace Chess {

struct PawnHashEntry {
    uint64_t key   = 0;
    int      mg    = 0;  // Middlegame pawn structure score
    int      eg    = 0;  // Endgame pawn structure score
    uint64_t wp_passers = 0;  // White passed pawn bitboard (b64+)
    uint64_t bp_passers = 0;  // Black passed pawn bitboard (b64+)
};

class PawnHashTable {
public:
    PawnHashTable(size_t size_kb = 256) {
        resize(size_kb);
    }

    void resize(size_t size_kb) {
        size_t bytes = size_kb * 1024;
        size_t num_entries = bytes / sizeof(PawnHashEntry);
        // Round down to power of 2 for fast masking
        mask_ = 1;
        while (mask_ * 2 <= num_entries) mask_ *= 2;
        mask_ -= 1;
        table_.resize(mask_ + 1);
        clear();
    }

    void clear() {
        for (auto& e : table_) {
            e.key = 0;
            e.mg  = 0;
            e.eg  = 0;
            e.wp_passers = 0;
            e.bp_passers = 0;
        }
    }

    bool probe(uint64_t key, int& mg, int& eg, uint64_t& wp_passers, uint64_t& bp_passers) const {
        const PawnHashEntry& e = table_[key & mask_];
        if (e.key == key) {
            mg = e.mg;
            eg = e.eg;
            wp_passers = e.wp_passers;
            bp_passers = e.bp_passers;
            return true;
        }
        return false;
    }

    void store(uint64_t key, int mg, int eg, uint64_t wp_passers, uint64_t bp_passers) {
        PawnHashEntry& e = table_[key & mask_];
        e.key = key;
        e.mg  = mg;
        e.eg  = eg;
        e.wp_passers = wp_passers;
        e.bp_passers = bp_passers;
    }

private:
    std::vector<PawnHashEntry> table_;
    size_t mask_ = 0;
};

// Global pawn hash table
extern PawnHashTable PawnTT;

} // namespace Chess