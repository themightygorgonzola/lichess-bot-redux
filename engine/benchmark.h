#pragma once

#include "search.h"
#include <iosfwd>
#include <string>
#include <vector>

namespace Chess::Benchmark {

struct SearchBenchPosition {
    std::string name;
    std::string fen;
};

struct SearchBenchConfig {
    int depth = 10;
    int threads = 1;
    int hash_mb = 64;
    int repeat = 1;
    int warmup_depth = 4;
    bool clear_tt_each_position = true;
    bool clear_history_each_position = true;
    bool collect_profile = false;
    SearchMode mode = LAZY_SMP;
};

struct SearchBenchCaseResult {
    std::string name;
    std::string fen;
    std::string bestmove;
    std::string ponder;
    int depth = 0;
    int seldepth = 0;
    int score_cp = 0;
    int64_t nodes = 0;
    int64_t elapsed_us = 0;
    int64_t elapsed_ms = 0;
    int64_t nps = 0;
    SearchSmpStats smp;
    bool has_profile = false;
    SearchProfile profile;
};

struct SearchBenchSummary {
    SearchBenchConfig config;
    std::vector<SearchBenchCaseResult> cases;
    int64_t total_nodes = 0;
    int64_t total_elapsed_us = 0;
    int64_t total_elapsed_ms = 0;
    int64_t total_nps = 0;
    int64_t median_nps = 0;
    int64_t min_nps = 0;
    int64_t max_nps = 0;
    bool has_profile = false;
    SearchProfile aggregate_profile;
};

std::vector<SearchBenchPosition> default_search_suite();
std::vector<SearchBenchPosition> load_search_suite_file(const std::string& path);

SearchBenchSummary run_search_benchmark(const std::vector<SearchBenchPosition>& suite,
                                        const SearchBenchConfig& config);

void print_search_benchmark_text(std::ostream& os, const SearchBenchSummary& summary);
void print_search_benchmark_json(std::ostream& os, const SearchBenchSummary& summary);

} // namespace Chess::Benchmark