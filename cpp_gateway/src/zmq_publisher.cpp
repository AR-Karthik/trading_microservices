#include "zmq_publisher.h"
#include <iostream>

ZmqPublisher::ZmqPublisher(const std::string& endpoint) 
    : context(1), publisher(context, ZMQ_PUB) {
    publisher.bind(endpoint);
    std::cout << "[ZMQ] PUB socket bound to " << endpoint << "\n";
}

void ZmqPublisher::publish(const trading::TickData& tick) {
    std::string binary_payload;
    tick.SerializeToString(&binary_payload);

    // Topic: TICK.{SYMBOL}
    std::string topic = "TICK." + tick.symbol();
    
    zmq::message_t topic_msg(topic.size());
    memcpy(topic_msg.data(), topic.data(), topic.size());
    
    zmq::message_t payload_msg(binary_payload.size());
    memcpy(payload_msg.data(), binary_payload.data(), binary_payload.size());

    // Send multi-part: [Topic, Payload]
    publisher.send(topic_msg, zmq::send_flags::sndmore);
    publisher.send(payload_msg, zmq::send_flags::none);
}
