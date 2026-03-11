# Stage 1: Build C++ Gateway
FROM python:3.11-slim AS builder-cpp

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    pkg-config \
    libzmq3-dev \
    libprotobuf-dev \
    protobuf-compiler \
    libhiredis-dev \
    libssl-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install simdjson
RUN git clone https://github.com/simdjson/simdjson.git /tmp/simdjson && \
    cd /tmp/simdjson && mkdir build && cd build && \
    cmake .. && make -j$(nproc) && make install

# Install redis-plus-plus
RUN git clone https://github.com/sewenew/redis-plus-plus.git /tmp/redis-pp && \
    cd /tmp/redis-pp && mkdir build && cd build && \
    cmake .. && make -j$(nproc) && make install

# Install uWebSockets & uSockets
RUN git clone --recursive https://github.com/uNetworking/uWebSockets.git /tmp/uWebSockets && \
    cd /tmp/uWebSockets/uSockets && make -j$(nproc) && cp uSockets.a /usr/local/lib/ && \
    mkdir -p /usr/local/include/uWebSockets && cp -r src/* /usr/local/include/uWebSockets/ && \
    mkdir -p /usr/local/include/uSockets && cp -r src/* /usr/local/include/uSockets/

WORKDIR /app
COPY cpp_gateway/ ./cpp_gateway/

# Generate Protobuf
RUN protoc -I=cpp_gateway/proto --cpp_out=cpp_gateway/proto cpp_gateway/proto/messages.proto

RUN mkdir -p cpp_gateway/build && cd cpp_gateway/build && \
    cmake .. && \
    make -j$(nproc)

# Stage 2: Runtime Image
FROM python:3.11-slim AS runtime

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libzmq5 \
    libprotobuf32 \
    libpq-dev \
    libssl3 \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled libraries from builder
COPY --from=builder-cpp /usr/local/lib/ /usr/local/lib/
RUN ldconfig

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Pre-built C++ Gateway Binary from builder stage
COPY --from=builder-cpp /app/cpp_gateway/build/cpp_gateway /usr/local/bin/
RUN chmod +x /usr/local/bin/cpp_gateway

# Copy the rest of the Python code
COPY . .

# Ensure /ram_disk exists for IPC
RUN mkdir -p /ram_disk

# Default command
CMD ["python", "-m", "daemons.data_gateway"]
