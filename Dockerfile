# Stage 1: Build Rust tick_engine
FROM rust:1.77-slim AS rust-builder
RUN apt-get update && apt-get install -y python3-pip python3-dev
RUN pip install maturin --break-system-packages
WORKDIR /build/rust_extensions/tick_engine
COPY rust_extensions/tick_engine .
RUN maturin build --release --out /wheels

# Stage 2: Build C++ Gateway
FROM gcc:13 AS cpp-builder
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libzmq3-dev \
    libprotobuf-dev \
    protobuf-compiler \
    libssl-dev \
    zlib1g-dev \
    libsimdjson-dev \
    libhiredis-dev \
    git \
    make \
    g++

# 1. Build redis-plus-plus
WORKDIR /build/deps
RUN git clone https://github.com/sewenew/redis-plus-plus.git && \
    cd redis-plus-plus && \
    mkdir build && cd build && \
    cmake .. -DREDIS_PLUS_PLUS_CXX_STANDARD=20 && \
    make -j$(nproc) && \
    make install

# 2. Build uSockets/uWebSockets
WORKDIR /build/deps
RUN git clone --recursive https://github.com/uNetworking/uWebSockets.git
WORKDIR /build/deps/uWebSockets/uSockets
RUN make && cp uSockets.a /usr/local/lib/

# 3. Build C++ Gateway
WORKDIR /build/cpp_gateway
COPY cpp_gateway .
RUN protoc -I=proto --cpp_out=proto proto/messages.proto
RUN ls -R /build/deps/uWebSockets/src && \
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build -- VERBOSE=1

# Stage 3: Python Runtime
FROM python:3.11-slim AS runtime
RUN apt-get update && apt-get install -y \
    libzmq5 \
    libprotobuf-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the Rust extension from wheel
COPY --from=rust-builder /wheels /wheels
RUN pip install /wheels/*.whl

# Copy the C++ binary
COPY --from=cpp-builder /build/cpp_gateway/build/cpp_gateway /usr/local/bin/

# Copy the rest of the Python code
COPY . .

# Default command
CMD ["python", "-m", "daemons.data_gateway"]
