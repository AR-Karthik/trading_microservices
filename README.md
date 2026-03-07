# Python Microservices Trading Architecture

A bespoke, fully decoupled microservices architecture in Python built for paper trading and executing advanced quantitative strategies.

## Features

- **Decoupled Architecture**: Separate `asyncio` daemons for Data Gathering, Strategy Inference, and Execution.
- **Ultra-low Latency**: Internally uses ZeroMQ (ZMQ) for microsecond-scale brokerless routing.
- **Data Vault**: Uses Redis for in-memory ultra-fast ticking and TimescaleDB (PostgreSQL) for robust structured logging of P&L and Executions.
- **Ephemeral Infrastructure**: Automated provisioning scripts to spin up GCP Spot VMs securely via Tailscale, drastically cutting cloud costs.
- **Real-time Monitoring**: Streamlit dashboard showing live P&L, Weekly/Monthly Analytics, and execution latency.

## Prerequisites

1. Python 3.9+
2. Docker and Docker Compose
3. GCP Service Account configured (`GCP_PROJECT_ID` in `.env`)
4. Tailscale Auth Key (`TAILSCALE_AUTH_KEY` in `.env`)

## Running Locally

To test the entire architecture on your local machine:

1. Copy `.env.example` to `.env` (or use the one provided during setup).
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the application stack (which spins up Docker containers and the Python daemons):
   ```bash
   python run_local.py
   ```
4. Access the dashboard at `http://localhost:8501`.

## Deployment to GCP

To deploy the architecture to a heavily discounted ephemeral c2-standard-4 Spot VM:

```bash
python -m infrastructure.gcp_provision
```

The server will wake up, install Docker and Tailscale, connect to your Zero-Trust network, and clone this repository to begin executing.

## GitHub Setup

This directory is already initialized as a Git repository. To push it to your account (`https://github.com/AR-Karthik`):

```bash
git remote add origin https://github.com/AR-Karthik/trading_microservices.git
git add .
git commit -m "Initial commit of trading architecture"
git push -u origin master
```
