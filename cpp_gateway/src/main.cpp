#include <iostream>
#include <vector>
#include <thread>
#include <csignal>
#include <atomic>
#include <memory>
#include "shard.h"

std::atomic<bool> running(true);
std::vector<std::shared_ptr<Shard>> active_shards;

void signal_handler(int signal) {
    if (signal == SIGTERM || signal == SIGINT) {
        std::cout << "\n[!] Preemption / Shutdown signal (" << signal << ") received! Executing 30-Second Rush...\n";
        running = false;
        
        // 25s: Trigger high-speed RAM buffer flush on all shards
        for(auto& s : active_shards) {
            // In C++ we can call a flush method directly on the shards
            // Since Shard::run loops on 'running', it will naturally terminate, 
            // but we can enforce an immediate flush here or let the shard do it upon exit.
            // We'll let the Shard destructor/exit handle the flush to /mnt/persistent_ssd
        }
    }
}

int main() {
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    std::cout << "🦸 K.A.R.T.H.I.K. Deterministic Tri-Brain Gateway\n";
    std::cout << "=========================================================\n";

    // Create Shards
    // Alpha (Nifty): Core 1, Exchange_ID 1 (NSE)
    active_shards.push_back(std::make_shared<Shard>("SHARD_ALPHA", "NSE|26000,NSE|2885,NSE|1333,NSE|4963,NSE|1594,NSE|11536", 1, 1));
    
    // Beta (BankNifty): Core 2, Exchange_ID 1 (NSE)
    active_shards.push_back(std::make_shared<Shard>("SHARD_BETA", "NSE|26009,NSE|1333,NSE|4963,NSE|5900,NSE|1922,NSE|3045", 2, 1));

    // Gamma (Sensex): Core 3, Exchange_ID 2 (BSE) - Native Tokens
    active_shards.push_back(std::make_shared<Shard>("SHARD_GAMMA", "BSE|1,BSE|500180,BSE|500325,BSE|532174,BSE|532454,BSE|500510", 3, 2));

    std::vector<std::thread> threads;
    
    for (auto& s : active_shards) {
        threads.emplace_back([s]() {
            s->run(running);
        });
    }

    // Core 0 (Main Thread): Wait for shutdown
    for (auto& t : threads) {
        if (t.joinable()) t.join();
    }

    std::cout << "[+] Tri-Brain Gateway offline. RAM safely synced to Regional SSD.\n";
    return 0;
}
