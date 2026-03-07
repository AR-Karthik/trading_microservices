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
    
    # Define the processes we need to run
    # Note: These paths will exist once we create the daemons
    processes = [
        ("Data Gateway", "python -m daemons.data_gateway"),
        ("Strategy Engine", "python -m daemons.strategy_engine"),
        ("Paper Bridge", "python -m daemons.paper_bridge"),
        ("Dashboard", "streamlit run dashboard/app.py")
    ]
    
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
