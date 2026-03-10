#ifndef SHARD_H
#define SHARD_H

#include <string>
#include <atomic>
#include <vector>

class Shard {
public:
    Shard(std::string id, std::string symbols, int target_core, int exchange_id);
    void run(std::atomic<bool>& global_running);

private:
    std::string shard_id;
    std::string symbols_list;
    int core_pin_id;
    int exchange_id;

    // Lock-Free IPC helpers
    void write_to_shared_memory(const std::string& serialized_tick);
    
    // SIGTERM Defense
    void flush_to_regional_ssd();
    std::vector<std::string> ram_tick_buffer;
};

#endif
