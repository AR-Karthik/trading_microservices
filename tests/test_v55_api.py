import requests
import time

def test_api():
    base_url = "http://localhost:8000"
    
    print("--- 1. Testing Health ---")
    try:
        r = requests.get(f"{base_url}/health")
        print(f"Health: {r.status_code} - {r.json()}")
    except Exception as e:
        print(f"Health failed: {e}")

    print("\n--- 2. Testing State (Signals) ---")
    try:
        r = requests.get(f"{base_url}/state")
        data = r.json()
        print(f"Alpha Score: {data.get('alpha_score')}")
        print(f"Signals Keys: {list(data.get('signals', {}).keys())}")
    except Exception as e:
        print(f"State failed: {e}")

    print("\n--- 3. Testing Signal History (v5.5) ---")
    try:
        r = requests.get(f"{base_url}/signals/history?limit=5")
        history = r.json()
        print(f"History Count: {len(history)}")
        if history:
            print(f"Latest History Item: {history[0]}")
    except Exception as e:
        print(f"History failed: {e}")

    print("\n--- 4. Testing Greek Sensitivity (v5.5) ---")
    try:
        r = requests.get(f"{base_url}/greeks/sensitivity")
        greeks = r.json()
        print(f"Greek Sensitivity: {greeks}")
    except Exception as e:
        print(f"Greeks failed: {e}")

if __name__ == "__main__":
    test_api()
