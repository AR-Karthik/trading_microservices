cmake_minimum_required(VERSION 3.20)
project(cpp_gateway VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Dependencies: Protobuf, ZeroMQ, simdjson, uWebSockets
find_package(Protobuf REQUIRED)
find_package(ZeroMQ REQUIRED)

# Include directories
target_include_directories(cpp_gateway PRIVATE include)
target_include_directories(cpp_gateway PRIVATE proto)
target_include_directories(cpp_gateway PRIVATE ${PROTOBUF_INCLUDE_DIRS})
target_include_directories(cpp_gateway PRIVATE ../../deps/uWebSockets/src)
target_include_directories(cpp_gateway PRIVATE ../../deps/uWebSockets/uSockets/src)
target_include_directories(cpp_gateway PRIVATE proto)

# Source files (Protobuf generated manually in Docker)
set(SOURCES
    src/main.cpp
    src/shard.cpp
    src/tick_normalizer.cpp
    src/zmq_publisher.cpp
    src/redis_writer.cpp
    proto/messages.pb.cc
)

add_executable(cpp_gateway ${SOURCES})

target_link_libraries(cpp_gateway
    PRIVATE
    ${PROTOBUF_LIBRARIES}
    zmq
    simdjson
    hiredis
    redis++
    ssl
    crypto
    z
    pthread
    dl
    /usr/local/lib/uSockets.a
)

# Deployment target: Linux/GCP
if(WIN32)
    message(STATUS "Note: C++ Gateway is optimized for Linux (uWebSockets epoll). Building for Windows may require mock/portable shims.")
endif()
