import subprocess
import time
import sys
import os

def start_services():
    print("Starting local Data Vault (Redis & TimescaleDB)...")
    try:
        # We only start the DBs in Docker; the daemons run as local processes
        subprocess.run(["docker-compose", "up", "-d", "redis", "timescaledb"], check=True)
    except Exception as e:
        print(f"Warning: Docker databases failed to start ({e}).")
        print("Continuing with local microservices (they may fail to connect to DB).")
        print("Please ensure Docker is installed and running if you need persistence.")
        
    print("Waiting for databases to initialize (Health Check)...")
    import socket
    
    def wait_for_port(port, name, timeout=30):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with socket.create_connection(("localhost", port), timeout=1):
                    print(f"  ✅ {name} (Port {port}) is ready.")
                    return True
            except (ConnectionRefusedError, socket.timeout):
                time.sleep(1)
        print(f"  ❌ Timeout waiting for {name} on port {port}.")
        return False

    redis_ready = wait_for_port(6379, "Redis")
    db_ready = wait_for_port(5432, "TimescaleDB")
    
    if not (redis_ready and db_ready):
        print("Warning: One or more databases are not responding. Microservices may crash.")
        time.sleep(2)

    print("Starting Resilient Python Microservices...")
    
    # Base services (Resilience & Logic)
    processes = [
        ("Market Sensor", f"{sys.executable} -m daemons.market_sensor"),
        ("Meta Router", f"{sys.executable} -m daemons.meta_router"),
        ("Liquidation Daemon", f"{sys.executable} -m daemons.liquidation_daemon"),
        ("Paper Bridge", f"{sys.executable} -m daemons.paper_bridge"),
        ("Dashboard API", f"{sys.executable} -m uvicorn dashboard.api.main:app --host 0.0.0.0 --port 8000"),
        ("Dashboard Frontend", f"{sys.executable} -m http.server 8501 --directory dashboard/frontend")
    ]
    
    # Strategy Daemons
    strategies = [
        ("Strat Gamma", f"{sys.executable} -m daemons.strat_gamma"),
        ("Strat Reversion", f"{sys.executable} -m daemons.strat_reversion"),
        ("Strat Expiry", f"{sys.executable} -m daemons.strat_expiry"),
        ("Strat EOD", f"{sys.executable} -m daemons.strat_eod_vwap")
    ]
    processes.extend(strategies)
    
    # Data Ingestion
    if len(sys.argv) > 1 and sys.argv[1] == '--live':
        print("Live Mode: Adding Shoonya Gateway...")
        processes.insert(0, ("Shoonya Gateway", f"{sys.executable} -m daemons.shoonya_gateway"))
    else:
        print("Paper Mode: Using Mock Data Gateway.")
        processes.insert(0, ("Mock Data Gateway", f"{sys.executable} -m daemons.data_gateway"))
    
    running_procs = []
    
    try:
        for name, cmd in processes:
            print(f"Starting {name}...")
            # Ensure current directory is in PYTHONPATH for module discovery
            env = os.environ.copy()
            env["PYTHONPATH"] = f".{os.pathsep}{env.get('PYTHONPATH', '')}"
            
            p = subprocess.Popen(cmd.split(), env=env)
            running_procs.append((name, p))
            
        print("\nAll resilient services started! View Dashboard for monitoring.")
        print("Press Ctrl+C to stop.\n")
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nStopping all services...")
        for name, p in running_procs:
            print(f"Terminating {name}...")
            p.terminate()
            p.wait()
            
        print("Stopping Docker containers...")
        subprocess.run(["docker-compose", "down"])
        print("Shutdown complete.")

if __name__ == "__main__":
    start_services()
