#include <iostream>
#include <vector>
#include <thread>
#include <csignal>
#include <atomic>
#include "shard.h"

std::atomic<bool> running(true);

void signal_handler(int signal) {
    std::cout << "\n[!] Shutdown signal (" << signal << ") received. Graceful exit initiated...\n";
    running = false;
}

int main() {
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    std::cout << "🦸 Institutional C++ Data Gateway [uWebSockets Sharded Engine]\n";
    std::cout << "==========================================================\n";

    // Shard configuration: (Shard ID, Port/Symbols range)
    // To meet institutional standards, we shard by instrument intensity.
    std::vector<std::thread> shards;
    
    // Shard A: High-priority Index Spot/Futures
    shards.emplace_back([]() { 
        Shard engine("SHARD_A", "NIFTY_SPOT,BANKNIFTY_SPOT,NIFTY_FUT");
        engine.run(running);
    });

    // Shard B: Options Chain Shard
    shards.emplace_back([]() {
        Shard engine("SHARD_B", "NIFTY_OPTIONS_CHAIN");
        engine.run(running);
    });

    for (auto& t : shards) {
        if (t.joinable()) t.join();
    }

    std::cout << "[+] System offline. Latency recorded in logs.\n";
    return 0;
}
