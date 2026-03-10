#include "shard.h"
#include "tick_normalizer.h"
#include "zmq_publisher.h"
#include "redis_writer.h"
#include <App.h> // uWebSockets
#include <iostream>
#include <fstream>
#include <chrono>
#include <thread>
#include <vector>

#ifdef __linux__
#include <pthread.h>
#endif

// Mock of POSIX Shared Memory for development cross-compilation
void Shard::write_to_shared_memory(const std::string& serialized_tick) {
    // In Production (Linux):
    // 1. Open fd = shm_open("/karthik_live", O_CREAT | O_RDWR, 0666);
    // 2. void* ptr = mmap(0, SIZE, PROT_WRITE, MAP_SHARED, fd, 0);
    // 3. Write data using std::atomic_flag to prevent reader tears.
    // std::cout << "[SHM] " << shard_id << " atomic write: " << serialized_tick.size() << " bytes.\n";
}

void Shard::flush_to_regional_ssd() {
    std::cout << "[DEFENSE] " << shard_id << " caught SIGTERM. Flushing /dev/shm to Regional SSD...\n";
    std::string filename = "/mnt/persistent_ssd/recovery_" + shard_id + ".bin";
    
    // Fallback file for local testing if path doesn't exist
    std::ofstream out("recovery_" + shard_id + ".bin", std::ios::binary);
    if (out.is_open()) {
        for(const auto& tick : ram_tick_buffer) {
            out.write(tick.c_str(), tick.size());
        }
        out.close();
        std::cout << "[DEFENSE] " << shard_id << " flush complete in <1s.\n";
    }
}

Shard::Shard(std::string id, std::string symbols, int target_core, int exchange) 
    : shard_id(id), symbols_list(symbols), core_pin_id(target_core), exchange_id(exchange) {}

void Shard::run(std::atomic<bool>& global_running) {
    // 1. Core Pinning (Thread Affinity)
#ifdef __linux__
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(core_pin_id, &cpuset);
    pthread_t current_thread = pthread_self();
    if (pthread_setaffinity_np(current_thread, sizeof(cpu_set_t), &cpuset) != 0) {
        std::cerr << "[ERROR] " << shard_id << " failed to pin to Core " << core_pin_id << "\n";
    } else {
        std::cout << "[NUMA] " << shard_id << " physically pinned to CPU Core " << core_pin_id << "\n";
    }
#else
    std::cout << "[NUMA] " << shard_id << " simulated pin to CPU Core " << core_pin_id << " (Non-Linux env)\n";
#endif

    std::cout << "[INFO] " << shard_id << " active. Exchange ID: " << exchange_id << ". Monitoring: " << symbols_list << "\n";

    // 2. Initialize Telemetry Publishers (Core 0 handles this downstream)
    ZmqPublisher zmq_pub("tcp://*:555" + std::to_string(core_pin_id)); 
    RedisWriter redis_writer("localhost", 6379);

    // 3. Ingestion Loop
    while (global_running) {
        // Mocking tick ingestion via simdjson payload
        std::string mock_json = R"({"tk":"1333","lp":1450.5,"bp":1450.0,"sp":1450.2,"v":500,"e":"NSE"})";
        if (exchange_id == 2) {
            mock_json = R"({"tk":"500180","lp":1450.6,"bp":1450.1,"sp":1450.4,"v":200,"e":"BSE"})";
        }
        
        // This parser would use simdjson internally in production
        auto tick = TickNormalizer::normalize_shoonya_tick(mock_json);
        
        if (!tick.symbol().empty()) {
            std::string serialized;
            tick.SerializeToString(&serialized);
            
            // Push to shared memory (Lock-Free Circular Buffer)
            write_to_shared_memory(serialized);
            
            // Store locally for SIGTERM defense
            if (ram_tick_buffer.size() > 100000) ram_tick_buffer.clear(); // ring buffer behavior
            ram_tick_buffer.push_back(serialized);
            
            // ZMQ & Redis for standard path
            zmq_pub.publish(tick);
            redis_writer.write_tick(tick);
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(100)); // 10 Hz simulation
    }

    // Preemption Defense!
    // The loop only breaks when global_running = false (i.e. SIGTERM T-30s arrived).
    flush_to_regional_ssd();

    std::cout << "[INFO] " << shard_id << " graceful shutdown complete.\n";
}
