import math
from scipy.stats import norm

class BlackScholes:
    """Institutional-grade Black-Scholes engine for option greeks."""
    
    @staticmethod
    def d1(S, K, T, r, sigma):
        return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))

    @staticmethod
    def d2(S, K, T, r, sigma):
        return BlackScholes.d1(S, K, T, r, sigma) - sigma * math.sqrt(T)

    @staticmethod
    def call_price(S, K, T, r, sigma):
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        d2 = BlackScholes.d2(S, K, T, r, sigma)
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)

    @staticmethod
    def put_price(S, K, T, r, sigma):
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        d2 = BlackScholes.d2(S, K, T, r, sigma)
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    @staticmethod
    def delta(S, K, T, r, sigma, option_type='call'):
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        if option_type.lower() == 'call':
            return norm.cdf(d1)
        else:
            return norm.cdf(d1) - 1

    @staticmethod
    def gamma(S, K, T, r, sigma):
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        return norm.pdf(d1) / (S * sigma * math.sqrt(T))

    @staticmethod
    def vega(S, K, T, r, sigma):
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        return S * norm.pdf(d1) * math.sqrt(T) / 100  # Per 1% change

    @staticmethod
    def theta(S, K, T, r, sigma, option_type='call'):
        d1 = BlackScholes.d1(S, K, T, r, sigma)
        d2 = BlackScholes.d2(S, K, T, r, sigma)
        common = -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
        if option_type.lower() == 'call':
            return (common - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365
        else:
            return (common + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365
