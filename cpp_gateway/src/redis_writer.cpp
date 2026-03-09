#include "redis_writer.h"
#include <iostream>
#include <vector>
#include <unordered_map>

RedisWriter::RedisWriter(const std::string& host, int port) 
    : redis("tcp://" + host + ":" + std::to_string(port)) {
    std::cout << "[Redis] Connected to " << host << ":" << port << "\n";
}

void RedisWriter::write_tick(const trading::TickData& tick) {
    // Key: tick:{SYMBOL}
    std::string key = "tick:" + tick.symbol();
    
    // Redis Stream entry: XADD tick:NIFTY50 MAXLEN ~ 100 * price 180.5 ...
    std::unordered_map<std::string, std::string> entry;
    entry["price"] = std::to_string(tick.price());
    entry["bid"] = std::to_string(tick.bid());
    entry["ask"] = std::to_string(tick.ask());
    entry["volume"] = std::to_string(tick.last_volume());
    entry["ts"] = tick.timestamp();

    try {
        redis.xadd(key, "*", entry.begin(), entry.end());
        // Enforce MAXLEN for efficient memory usage
        // Note: redis-plus-plus raw command for MAXLEN trimming
        redis.command("XTRIM", key, "MAXLEN", "~", "100");
    } catch (const std::exception& e) {
        std::cerr << "[Redis] Write error: " << e.what() << "\n";
    }
}
