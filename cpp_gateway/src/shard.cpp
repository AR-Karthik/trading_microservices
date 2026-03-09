#include "shard.h"
#include "tick_normalizer.h"
#include "zmq_publisher.h"
#include "redis_writer.h"
#include "../../deps/uWebSockets/src/App.h" // uWebSockets
#include <iostream>
#include <chrono>

/*
 * Implementation Note:
 * This shard uses uWebSockets (Client) to connect to the Shoonya binary feed.
 * It uses simdjson to parse the feed and TickNormalizer to convert to Protobuf.
 * Finally, it broadcasts to the Python Market Sensor via ZeroMQ.
 */

Shard::Shard(std::string id, std::string symbols) : shard_id(id), symbols_list(symbols) {}

void Shard::run(std::atomic<bool>& global_running) {
    std::cout << "[INFO] " << shard_id << " active. Shard monitoring: " << symbols_list << "\n";

    // 1. Initialize Publishers
    ZmqPublisher zmq_pub("tcp://*:5557"); // Shard-specific or shared port
    RedisWriter redis_writer("localhost", 6379);

    // 2. uWebSockets Loop
    // For local dev, we use a mock loop since we don't have the Shoonya WS endpoint here
    // In production, this would be: 
    // uWS::App().ws<UserData>("/*", { ... }).run();

    while (global_running) {
        // Mocking tick ingestion for system verification
        std::string mock_json = R"({"tk":"NIFTY50","lp":22100.5,"bp":22100.0,"sp":22101.0,"v":150})";
        
        auto tick = TickNormalizer::normalize_shoonya_tick(mock_json);
        
        if (!tick.symbol().empty()) {
            zmq_pub.publish(tick);
            redis_writer.write_tick(tick);
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(10)); // 100 Hz simulation
    }

    std::cout << "[INFO] " << shard_id << " graceful shutdown complete.\n";
}
