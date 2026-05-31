#pragma once

#include "types.h"
#include "move.h"
#include <atomic>
#include <array>
#include <memory>
#include <cstring>
#include <mutex>
#ifdef _MSC_VER
#include <intrin.h>
#endif

// ============================================================================
// Transposition Table — shared hash table for Lazy SMP multithreading
// Uses lock-free atomic operations for thread safety.
// ============================================================================

namespace Chess {

enum TTFlag : uint8_t {
    TT_NONE  = 0,
    TT_EXACT = 1,  // Exact score
    TT_ALPHA = 2,  // Upper bound (failed low)
    TT_BETA  = 3   // Lower bound (failed high)
};

struct TTEntry {
    Key      key   = 0;
    Move     move  = Move::none();
    int16_t  score = 0;
    int8_t   depth = 0;
    TTFlag   flag  = TT_NONE;
    uint8_t  age   = 0;

    bool is_valid() const { return flag != TT_NONE; }
};

class TranspositionTable {
public:
    TranspositionTable() = default;
    ~TranspositionTable();

    // Resize TT (size in MB)
    void resize(size_t mb);

    // Clear all entries
    void clear();

    // Probe the TT for a position
    // Returns true if a matching entry was found
    bool probe(Key key, TTEntry& entry) const;

    // Store a new entry
    void store(Key key, Move move, int score, int depth, TTFlag flag);

    // Prefetch the bucket for a key (warms the cache for an upcoming probe)
    void prefetch(Key key) const {
        if (table_) {
#if defined(__GNUC__) || defined(__clang__)
            __builtin_prefetch(&table_[key & (size_ - 1)]);
#elif defined(_MSC_VER)
            _mm_prefetch(reinterpret_cast<const char*>(&table_[key & (size_ - 1)]), _MM_HINT_T0);
#endif
        }
    }

    // Get fill rate (approximate, for info display)
    int hashfull() const;

    // Increment age (called at the start of each new search)
    void new_search() { age_ = (age_ + 1) & 0xFF; }

private:
    // ── Lockless XOR-validated slot (Crafty/Stockfish trick) ─────────────────
    //   stored_key = real_key ^ data
    // On probe, load both relaxed; valid iff (stored ^ data) == real_key.
    // A torn read fails the XOR check with probability ~1/2^64.
    //
    // data layout (uint64_t):
    //   bits  0..15  move.raw()  (uint16_t)
    //   bits 16..31  score       (int16_t bit-cast)
    //   bits 32..39  depth       (int8_t  bit-cast)
    //   bits 40..47  flag        (uint8_t)
    //   bits 48..55  age         (uint8_t)
    //   bits 56..63  reserved
    struct Slot {
        std::atomic<uint64_t> key_xor;
        std::atomic<uint64_t> data;
    };
    static_assert(sizeof(Slot) == 16, "TT Slot must be 16 bytes");

    struct alignas(64) Bucket {
        Slot slots[4]; // 4-way set-associative, exactly one cache line
    };
    static_assert(sizeof(Bucket) == 64, "TT Bucket must be 64 bytes");

    static uint64_t pack(Move move, int score, int depth, TTFlag flag, uint8_t age) {
        uint64_t d = uint64_t(uint16_t(move.raw()));
        d |= uint64_t(uint16_t(int16_t(score))) << 16;
        d |= uint64_t(uint8_t(int8_t(depth)))    << 32;
        d |= uint64_t(uint8_t(flag))             << 40;
        d |= uint64_t(age)                       << 48;
        return d;
    }
    static void unpack(uint64_t d, TTEntry& e) {
        e.move  = Move(uint16_t(d & 0xFFFF));
        e.score = int16_t(uint16_t((d >> 16) & 0xFFFF));
        e.depth = int8_t (uint8_t ((d >> 32) & 0xFF));
        e.flag  = TTFlag (uint8_t ((d >> 40) & 0xFF));
        e.age   = uint8_t((d >> 48) & 0xFF);
    }

    size_t bucket_index(Key key) const { return key & (size_ - 1); }

    mutable std::mutex table_mutex_;   // only for resize/clear
    Bucket* table_  = nullptr;
    size_t  size_    = 0;     // Number of buckets
    uint8_t age_     = 0;
};

// Global TT instance (shared across all search threads)
extern TranspositionTable TT;

} // namespace Chess
