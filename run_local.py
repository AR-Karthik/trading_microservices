import subprocess
import time
import sys
import os

def start_services():
    print("Starting local Data Vault (Redis & TimescaleDB)...")
    try:
        subprocess.run(["docker-compose", "up", "-d"], check=True)
    except FileNotFoundError:
        print("docker-compose not found. Please ensure Docker is installed and running.")
        sys.exit(1)
        
    print("Waiting for databases to initialize...")
    time.sleep(5)

    print("Starting Python microservices...")
    
    processes = [
        ("Strategy Engine", f"{sys.executable} -m daemons.strategy_engine"),
        ("Paper Bridge", f"{sys.executable} -m daemons.paper_bridge"),
        ("Dashboard", f"{sys.executable} -m streamlit run dashboard/app.py --server.headless true")
    ]
    
    if len(sys.argv) > 1 and sys.argv[1] == '--live':
        print("🟢 Live Mode Requested: Adding Shoonya Gateway and Live execution bridge...")
        processes.insert(0, ("Shoonya Gateway", f"{sys.executable} -m daemons.shoonya_gateway"))
        processes.insert(3, ("Live Bridge", f"{sys.executable} -m daemons.live_bridge"))
    else:
        print("📄 Paper Mode: Using Mock Data Gateway.")
        processes.insert(0, ("Mock Data Gateway", f"{sys.executable} -m daemons.data_gateway"))
    
    running_procs = []
    
    try:
        for name, cmd in processes:
            print(f"Starting {name}...")
            # Using Popen to run asynchronously
            p = subprocess.Popen(cmd.split(), env=os.environ.copy())
            running_procs.append((name, p))
            
        print("\nAll services started! Press Ctrl+C to stop.")
        
        # Keep main thread alive
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
