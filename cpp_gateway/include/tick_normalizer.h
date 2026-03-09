#ifndef TICK_NORMALIZER_H
#define TICK_NORMALIZER_H

#include <string>
#include "messages.pb.h"

class TickNormalizer {
public:
    static trading::TickData normalize_shoonya_tick(const std::string& raw_payload);
};

#endif
