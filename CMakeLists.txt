cmake_minimum_required(VERSION 3.20)
project(Redux VERSION 1.0.0 LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Compiler-specific optimization flags
option(PROFILE "Build with rdtsc cycle-counter profiling (disables LTO)" OFF)

if(MSVC)
    if(PROFILE)
        add_compile_options(/W4 /O2 /Zi /Oy- /arch:AVX2)
        add_compile_definitions(PROFILE)
        message(STATUS "Profiling build enabled (rdtsc counters) [MSVC]")
    else()
        add_compile_options(/W4 /O2 /arch:AVX2 /GL /fp:fast)
        add_link_options(/LTCG)
    endif()
    add_compile_definitions(_CRT_SECURE_NO_WARNINGS)
else()
    # Use gcc-ar / gcc-ranlib so LTO plugin is available during archiving
    if(CMAKE_CXX_COMPILER_AR)
        set(CMAKE_AR     "${CMAKE_CXX_COMPILER_AR}")
    endif()
    if(CMAKE_CXX_COMPILER_RANLIB)
        set(CMAKE_RANLIB "${CMAKE_CXX_COMPILER_RANLIB}")
    endif()

    if(PROFILE)
        add_compile_options(-Wall -O2 -march=native -pg -g)
        add_compile_definitions(PROFILE)
        add_link_options(-pg)
        message(STATUS "Profiling build enabled (rdtsc counters + gprof)")
    else()
        add_compile_options(-Wall -Wextra -Wpedantic -O3 -march=native -flto)
        add_link_options(-flto)
    endif()
endif()

# ── Output directory — engine binary lands next to the bot for easy packaging ──
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/bot/engine")
# MSVC multi-config generators need per-config variants
foreach(_cfg Release RelWithDebInfo MinSizeRel Debug)
    string(TOUPPER "${_cfg}" _CFG)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${_CFG} "${CMAKE_SOURCE_DIR}/bot/engine")
endforeach()

# Threading
find_package(Threads REQUIRED)

# Engine sources
set(ENGINE_SOURCES
    engine/main.cpp
    engine/bitboard.cpp
    engine/board.cpp
    engine/benchmark.cpp
    engine/movegen.cpp
    engine/search.cpp
    engine/eval.cpp
    engine/eval_params.cpp
    engine/tt.cpp
    engine/uci.cpp
    engine/pawn_hash.cpp
    engine/nnue/nnue_network.cpp
    engine/nnue/nnue_simd.cpp
    engine/nnue/nnue_updater.cpp
    engine/nnue/nnue_eval.cpp
    engine/syzygy/syzygy.cpp
    engine/syzygy/tbprobe.c
)

set(ENGINE_HEADERS
    engine/types.h
    engine/bitboard.h
    engine/board.h
    engine/benchmark.h
    engine/move.h
    engine/movegen.h
    engine/search.h
    engine/eval.h
    engine/eval_params.h
    engine/tt.h
    engine/uci.h
    engine/pawn_hash.h
    engine/nnue/nnue_arch.h
    engine/nnue/nnue_arch_v2.h
    engine/nnue/nnue_accumulator.h
    engine/nnue/nnue_network.h
    engine/nnue/nnue_updater.h
    engine/nnue/nnue_eval.h
    engine/syzygy/syzygy.h
    engine/syzygy/tbprobe.h
    engine/syzygy/tbconfig.h
    engine/syzygy/stdendian.h
)

add_executable(redux-nnue ${ENGINE_SOURCES} ${ENGINE_HEADERS})
target_include_directories(redux-nnue PRIVATE engine engine/syzygy)
target_link_libraries(redux-nnue PRIVATE Threads::Threads)

# ── HCE-only build (same sources, DISABLE_NNUE blocks auto-load + EvalFile) ────
add_executable(redux-hce ${ENGINE_SOURCES} ${ENGINE_HEADERS})
target_compile_definitions(redux-hce PRIVATE DISABLE_NNUE)
target_include_directories(redux-hce PRIVATE engine engine/syzygy)
target_link_libraries(redux-hce PRIVATE Threads::Threads)

# Optional: build in debug mode
if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    if(MSVC)
        target_compile_options(redux-nnue PRIVATE /Od /Zi)
    else()
        target_compile_options(redux-nnue PRIVATE -g -O0 -fsanitize=address,undefined)
        target_link_options(redux-nnue PRIVATE -fsanitize=address,undefined)
    endif()
endif()
