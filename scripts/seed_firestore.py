import os
from google.cloud import firestore

def seed():
    project_id = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
    db = firestore.Client(project=project_id)
    
    # 1. System Metadata
    meta_ref = db.collection("system").document("metadata")
    if not meta_ref.get().exists:
        print("Seeding system/metadata...")
        meta_ref.set({
            "status": "OFFLINE",
            "vm_public_ip": None,
            "last_heartbeat": None,
            "live_alpha": 0.0,
            "live_regime": "UNKNOWN",
            "updated_at": firestore.SERVER_TIMESTAMP
        })
    else:
        print("system/metadata already exists.")

    # 2. System Config
    cfg_ref = db.collection("system").document("config")
    if not cfg_ref.get().exists:
        print("Seeding system/config...")
        cfg_ref.set({
            "paper_capital_limit": 1000000.0,
            "live_capital_limit": 0.0,
            "hurst_threshold": 0.55,
            "hybrid_confidence": 0.70,
            "vpin_toxicity": 0.82,
            "max_risk_per_trade_paper": 2500.0,
            "max_risk_per_trade_live": 2500.0,
            "updated_at": firestore.SERVER_TIMESTAMP
        })
    else:
        print("system/config already exists.")

if __name__ == "__main__":
    seed()
