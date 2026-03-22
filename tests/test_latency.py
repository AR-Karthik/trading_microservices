import time
import numpy as np
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger("LatencyTest")


    """Mock test of fractional kelly math in python."""
    state = {"spot": 22100}
    regime = {"regime": "TRENDING", "prob": 0.85}
    
    start = time.perf_counter_ns()
    # Math simulation
    p = regime["prob"]
    b = 1.5
    f = max(0.01, 0.5 * (p - ((1 - p) / b)))
    w = min(0.40, f)
    end = time.perf_counter_ns()
    
    latency_ns = end - start
    logger.info(f"[Router Math] Fractional Kelly Evaluation -> Latency: {latency_ns} ns")

if __name__ == "__main__":
    logger.info("Starting Phase 2 Micro-Latency Profiling...\n")
    profile_router_decision()
