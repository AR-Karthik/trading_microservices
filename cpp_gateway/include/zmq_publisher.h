#ifndef ZMQ_PUBLISHER_H
#define ZMQ_PUBLISHER_H

#include <string>
#include <zmq.hpp>
#include "proto/messages.pb.h"

class ZmqPublisher {
public:
    ZmqPublisher(const std::string& endpoint);
    void publish(const trading::TickData& tick);

private:
    zmq::context_t context;
    zmq::socket_t publisher;
};

#endif
