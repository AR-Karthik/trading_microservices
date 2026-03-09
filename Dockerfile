# Optimized Runtime Image for Project K.A.R.T.H.I.K. V5.4
FROM python:3.11-slim AS runtime

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libzmq5 \
    libprotobuf-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Pre-built Rust extensions (assumes wheels are in ./wheels on host)
COPY wheels/*.whl /tmp/
RUN pip install /tmp/*.whl && rm -rf /tmp/*.whl

# Copy Pre-built C++ Gateway Binary (assumes binary is at ./cpp_gateway/build/cpp_gateway on host)
COPY cpp_gateway/build/cpp_gateway /usr/local/bin/
RUN chmod +x /usr/local/bin/cpp_gateway

# Copy the rest of the Python code
COPY . .

# Default command
CMD ["python", "-m", "daemons.data_gateway"]
