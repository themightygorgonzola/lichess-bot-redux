#include "tt.h"
#include <cstdlib>
#include <cstring>
#include <iostream>

namespace Chess {

TranspositionTable TT;

TranspositionTable::~TranspositionTable() {
    std::free(table_);
}

void TranspositionTable::resize(size_t mb) {
    std::lock_guard<std::mutex> table_lock(table_mutex_);
    std::free(table_);

    size_ = (mb * 1024 * 1024) / sizeof(Bucket);
    if (size_ == 0) size_ = 1;

    // Round down to power of 2 for fast modulo via bitmask
    size_t s = 1;
    while (s * 2 <= size_) s *= 2;
    size_ = s;

    // calloc gives zeroed memory: key_xor=0, data=0 → unpacked flag=TT_NONE.
    table_ = static_cast<Bucket*>(std::calloc(size_, sizeof(Bucket)));
    if (!table_) {
        std::cerr << "Failed to allocate " << mb << " MB for transposition table\n";
        size_ = 0;
    }
    age_ = 0;
}

void TranspositionTable::clear() {
    std::lock_guard<std::mutex> table_lock(table_mutex_);
    if (table_) std::memset(table_, 0, size_ * sizeof(Bucket));
    age_ = 0;
}

bool TranspositionTable::probe(Key key, TTEntry& entry) const {
    if (!table_ || size_ == 0) return false;

    const Bucket& bucket = table_[bucket_index(key)];
    for (int i = 0; i < 4; ++i) {
        uint64_t kx = bucket.slots[i].key_xor.load(std::memory_order_relaxed);
        uint64_t d  = bucket.slots[i].data.load(std::memory_order_relaxed);
        if ((kx ^ d) == key) {
            unpack(d, entry);
            if (entry.flag != TT_NONE) {
                entry.key = key;
                return true;
            }
        }
    }
    return false;
}

void TranspositionTable::store(Key key, Move move, int score, int depth, TTFlag flag) {
    if (!table_ || size_ == 0) return;

    Bucket& bucket = table_[bucket_index(key)];

    // Pick replacement: prefer same-key or empty slot; else lowest depth/oldest.
    int  replace_idx    = 0;
    int  worst_score    = INT32_MAX;
    Move existing_move  = Move::none();
    int  existing_depth = -128;
    TTFlag existing_flag = TT_NONE;
    bool found_target   = false;

    for (int i = 0; i < 4; ++i) {
        uint64_t kx = bucket.slots[i].key_xor.load(std::memory_order_relaxed);
        uint64_t d  = bucket.slots[i].data.load(std::memory_order_relaxed);

        TTEntry e;
        unpack(d, e);
        bool same_key = (e.flag != TT_NONE) && ((kx ^ d) == key);
        bool empty    = (e.flag == TT_NONE);

        if (same_key) {
            replace_idx    = i;
            existing_move  = e.move;
            existing_depth = e.depth;
            existing_flag  = e.flag;
            found_target   = true;
            break;
        }
        if (empty && !found_target) {
            replace_idx  = i;
            found_target = true;
            continue;
        }
        if (!found_target) {
            int rs = e.depth * 256 - ((age_ - e.age) & 0xFF) * 2;
            if (rs < worst_score) { worst_score = rs; replace_idx = i; }
        }
    }

    // Don't overwrite a deeper same-key entry with shallower non-EXACT data
    // unless we have a new best move to record.
    if (existing_flag != TT_NONE && depth < existing_depth
        && flag != TT_EXACT && !move) {
        return;
    }

    // Keep the old move if we don't have a new one
    Move final_move = move ? move : existing_move;

    uint64_t d  = pack(final_move, score, depth, flag, age_);
    uint64_t kx = Key(key) ^ d;
    bucket.slots[replace_idx].data.store(d, std::memory_order_relaxed);
    bucket.slots[replace_idx].key_xor.store(kx, std::memory_order_relaxed);
}

int TranspositionTable::hashfull() const {
    if (!table_ || size_ == 0) return 0;

    int filled = 0;
    size_t sample = std::min(size_, size_t(1000));
    for (size_t i = 0; i < sample; ++i) {
        for (int j = 0; j < 4; ++j) {
            uint64_t d = table_[i].slots[j].data.load(std::memory_order_relaxed);
            TTEntry e;
            unpack(d, e);
            if (e.flag != TT_NONE && e.age == age_)
                ++filled;
        }
    }
    return filled * 1000 / (int(sample) * 4);
}

} // namespace Chess

