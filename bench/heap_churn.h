// Copyright (c) The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#ifndef VAROPS_BENCH_HEAP_CHURN_H
#define VAROPS_BENCH_HEAP_CHURN_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <numeric>
#include <random>
#include <vector>

//! A heap fragmented as a long-running node's may be, for allocations made while
//! it lives. Small values of mixed sizes are allocated, then freed and replaced in
//! random order over several rounds, with a large allocation between rounds (as a
//! block buffer is), which makes allocators such as glibc's merge free neighbours.
//! Half of the values are finally freed, in random order, and the rest stay live,
//! so later small allocations fill the holes they leave before fresh memory.
class HeapChurn
{
public:
    explicit HeapChurn(uint64_t seed, size_t slots = 3'000'000, int rounds = 3)
        : m_live(slots)
    {
        std::mt19937_64 rng{seed};
        std::uniform_int_distribution<size_t> size{1, 96};
        std::vector<size_t> order(slots);
        std::iota(order.begin(), order.end(), size_t{0});
        for (auto& value : m_live) value.assign(size(rng), 0x5a);
        const auto free_half{[&] {
            std::shuffle(order.begin(), order.end(), rng);
            for (size_t i{0}; i < slots / 2; ++i) std::vector<unsigned char>().swap(m_live[order[i]]);
            std::vector<unsigned char> large(size_t{4} << 20, 0x5a);
            m_sink += large[rng() % large.size()];
        }};
        for (int round{0}; round < rounds; ++round) {
            free_half();
            for (size_t i{0}; i < slots / 2; ++i) m_live[order[i]].assign(size(rng), 0x5a);
        }
        free_half();
    }

private:
    std::vector<std::vector<unsigned char>> m_live;
    unsigned m_sink{0};
};

#endif // VAROPS_BENCH_HEAP_CHURN_H
