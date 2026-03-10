import time
import numpy as np
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger("LatencyTest")

def profile_hmm_inference():
    """Mock test of GMM-HMM inference latency on a 300x4 matrix."""
    try:
        from hmmlearn.hmm import GMMHMM
        
        # Create dummy model
        model = GMMHMM(n_components=3, n_mix=2, covariance_type="diag", n_iter=10)
        X_train = np.random.randn(1000, 4)
        model.fit(X_train)
        
        # Test latency
        X_test = np.random.randn(300, 4)
        
        start = time.perf_counter_ns()
        _, state_seq = model.decode(X_test, lengths=[len(X_test)])
        probas = model.predict_proba(X_test)
        end = time.perf_counter_ns()
        
        latency_us = (end - start) / 1000.0
        logger.info(f"[HMM Inference] matrix size=300x4 -> Latency: {latency_us:.2f} us")
        
        if latency_us > 500:
            logger.warning("FAILED validation: Inference exceeds 500us budget.")
        else:
            logger.info("PASS: Inference is well within 500us budget.")
            
    except ImportError:
        logger.info("hmmlearn not installed. Skipping HMM profiler.")

def profile_router_decision():
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
    profile_hmm_inference()
    profile_router_decision()
