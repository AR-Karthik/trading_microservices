import asyncio
import json
import logging
import os
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RegimeVerifier")

async def verify_orchestrator():
    """Validates the Regime Orchestrator and Signal Attribution flow."""
    redis_host = os.getenv("REDIS_HOST", "localhost")
    r = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
    
    logger.info("--- Phase 1: Engine Hot-Swap Verification ---")
    
    # 1. Set engine to HMM
    await r.set("active_regime_engine", "HMM")
    logger.info("Sent: active_regime_engine = HMM")
    await asyncio.sleep(2) # Wait for MetaRouter to sync
    
    # 2. Check latest attributions
    attr_raw = await r.get("latest_attributions")
    if attr_raw:
        attributions = json.loads(attr_raw)
        active_engine = attributions[0].get("active_engine")
        logger.info(f"Verified Active Engine in Attribution: {active_engine}")
        if active_engine == "HMM":
            logger.info("✅ SUCCESS: MetaRouter correctly synced to HMM mode.")
        else:
            logger.error(f"❌ FAILURE: Expected HMM, got {active_engine}")
    else:
        logger.warning("⚠️ No attributions found in Redis yet.")

    logger.info("\n--- Phase 2: Hybrid Consensus Verification ---")
    
    # 3. Set engine to HYBRID
    await r.set("active_regime_engine", "HYBRID")
    logger.info("Sent: active_regime_engine = HYBRID")
    await asyncio.sleep(2)
    
    attr_raw = await r.get("latest_attributions")
    if attr_raw:
        attributions = json.loads(attr_raw)
        active_engine = attributions[0].get("active_engine")
        logger.info(f"Verified Active Engine in Attribution: {active_engine}")
        if active_engine == "HYBRID":
            logger.info("✅ SUCCESS: MetaRouter correctly synced to HYBRID mode.")
        else:
            logger.error(f"❌ FAILURE: Expected HYBRID, got {active_engine}")
    
    logger.info("\n--- Phase 3: Shadow Logging Integrity ---")
    if attr_raw:
        decisions = attributions[0].get("decisions", {})
        engines = list(decisions.keys())
        logger.info(f"Engines captured in Shadow Log: {engines}")
        if set(["HMM", "DETERMINISTIC", "HYBRID"]).issubset(set(engines)):
            logger.info("✅ SUCCESS: Shadow logging is capturing all three engines.")
        else:
            logger.error(f"❌ FAILURE: Missing engines in shadow log.")

if __name__ == "__main__":
    asyncio.run(verify_orchestrator())
