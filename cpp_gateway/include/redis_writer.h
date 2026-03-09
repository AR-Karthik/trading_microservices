#ifndef REDIS_WRITER_H
#define REDIS_WRITER_H

#include <string>
#include <sw/redis++/redis++.h>
#include "proto/messages.pb.h"

class RedisWriter {
public:
    RedisWriter(const std::string& host, int port);
    void write_tick(const trading::TickData& tick);

private:
    sw::redis::Redis redis;
};

#endif
