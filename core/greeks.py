# NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
import math
# Removed scipy.stats.norm to eliminate 50MB import allocation and per-call GC array creation spikes

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf. Zero allocation, ~20ns vs ~200ns for scipy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _norm_pdf(x: float) -> float:
    """Standard normal PDF. Pure math, no array allocation."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

class BlackScholes:
    """Institutional-grade Black-Scholes engine for option greeks."""
    
    @staticmethod
    def d1(S, K, T, r, sigma):
        # #94: Parameter validation
        if S <= 0 or K <= 0:
            return 0.0
        # #95: Zero-division guard for T and sigma
        T_safe = max(T, 1e-9)
        sigma_safe = max(sigma, 1e-6)
        return (math.log(S / K) + (r + 0.5 * sigma_safe ** 2) * T_safe) / (sigma_safe * math.sqrt(T_safe))

    @staticmethod
    def d2(S, K, T, r, sigma):
        T_safe = max(T, 1e-9)
        sigma_safe = max(sigma, 1e-6)
        return BlackScholes.d1(S, K, T, r, sigma) - sigma_safe * math.sqrt(T_safe)

    @staticmethod
    def call_price(S, K, T, r, sigma):
        if T <= 0: return max(0.0, S - K)
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        d2 = BlackScholes.d2(S, K, T, r, sigma)
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)

    @staticmethod
    def put_price(S, K, T, r, sigma):
        if T <= 0: return max(0.0, K - S)
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        d2 = BlackScholes.d2(S, K, T, r, sigma)
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

    @staticmethod
    def delta(S, K, T, r, sigma, option_type='call'):
        if T <= 0:
            if option_type.lower() == 'call':
                return 1.0 if S > K else 0.0
            else:
                return -1.0 if S < K else 0.0
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        if option_type.lower() == 'call':
            return _norm_cdf(d1)
        else:
            return _norm_cdf(d1) - 1

    @staticmethod
    def gamma(S, K, T, r, sigma):
        if T <= 0 or S <= 0: return 0.0
        sigma_safe = max(sigma, 1e-6)
        T_safe = max(T, 1e-9)
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        return _norm_pdf(d1) / (S * sigma_safe * math.sqrt(T_safe))

    @staticmethod
    def vega(S, K, T, r, sigma):
        """Vega: price sensitivity to a 1 PERCENTAGE POINT change in IV.
        E.g., if IV moves from 15% to 16%, the option price changes by vega().
        NOT per-absolute-point (that would be 100x larger).
        """
        # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
        if T <= 0: return 0.0
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        return S * _norm_pdf(d1) * math.sqrt(T) / 100  # Per 1% change

    @staticmethod
    def theta(S, K, T, r, sigma, option_type='call'):
        # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
        if T <= 0: return 0.0
        T_safe = max(T, 1e-9)
        sigma_safe = max(sigma, 1e-6)
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        d2 = BlackScholes.d2(S, K, T, r, sigma)
        common = -(S * _norm_pdf(d1) * sigma_safe) / (2 * math.sqrt(T_safe))
        if option_type.lower() == 'call':
            return (common - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 252
        else:
            return (common + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 252
