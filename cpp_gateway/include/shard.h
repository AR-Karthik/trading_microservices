#ifndef SHARD_H
#define SHARD_H

#include <string>
#include <atomic>

class Shard {
public:
    Shard(std::string id, std::string symbols);
    void run(std::atomic<bool>& global_running);

private:
    std::string shard_id;
    std::string symbols_list;
};

#endif
