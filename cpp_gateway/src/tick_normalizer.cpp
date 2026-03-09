#include "tick_normalizer.h"
#include "simdjson.h"
#include <iostream>
#include <chrono>

using namespace simdjson;

trading::TickData TickNormalizer::normalize_shoonya_tick(const std::string& raw_payload) {
    trading::TickData tick;
    
    // Performance: Using SIMD-accelerated JSON parser
    dom::parser parser;
    dom::element doc;
    
    auto error = parser.parse(raw_payload).get(doc);
    if (error) {
        std::cerr << "[ERROR] JSON Parse failed: " << error << "\n";
        return tick;
    }

    try {
        // Field mapping from Shoonya/Binary-JSON spec
        tick.set_symbol((std::string)doc["tk"]);
        tick.set_price((double)doc["lp"]);
        tick.set_bid((double)doc["bp"]);
        tick.set_ask((double)doc["sp"]);
        tick.set_last_volume((int64_t)doc["v"]);
        
        // Add ISO timestamp for downstream audit
        auto now = std::chrono::system_clock::now();
        std::time_t now_c = std::chrono::system_clock::to_time_t(now);
        char buf[30];
        std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&now_c));
        tick.set_timestamp(buf);

    } catch (const std::exception& e) {
        std::cerr << "[ERROR] Field mapping error: " << e.what() << "\n";
    }

    return tick;
}
