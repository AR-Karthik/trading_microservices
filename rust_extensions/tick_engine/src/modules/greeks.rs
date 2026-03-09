/// Black-Scholes Greeks implementation in Rust
/// S: Spot Price, K: Strike Price, T: Time to expiry (years), r: Risk-free rate, sigma: Volatility

pub struct Greeks {
    pub delta: f64,
    pub gamma: f64,
    pub vega: f64,
    pub theta: f64,
    pub vanna: f64,
    pub charm: f64,
}

use statrs::distribution::{ContinuousCDF, Normal};

pub fn calculate_greeks(s: f64, k: f64, t: f64, r: f64, sigma: f64, is_call: bool) -> Greeks {
    if t <= 0.0 {
        return Greeks { delta: 0.0, gamma: 0.0, vega: 0.0, theta: 0.0, vanna: 0.0, charm: 0.0 };
    }

    let n = Normal::new(0.0, 1.0).unwrap();
    let d1 = ( (s / k).ln() + (r + 0.5 * sigma.powi(2)) * t ) / (sigma * t.sqrt());
    let d2 = d1 - sigma * t.sqrt();

    let n_prime_d1 = (-0.5 * d1.powi(2)).exp() / (2.0 * std::f64::consts::PI).sqrt();

    let delta = if is_call {
        n.cdf(d1)
    } else {
        n.cdf(d1) - 1.0
    };

    let gamma = n_prime_d1 / (s * sigma * t.sqrt());
    let vega = s * n_prime_d1 * t.sqrt();
    
    let theta_part1 = -(s * n_prime_d1 * sigma) / (2.0 * t.sqrt());
    let theta_part2 = r * k * (-r * t).exp();
    
    let theta = if is_call {
        theta_part1 - theta_part2 * n.cdf(d2)
    } else {
        theta_part1 + theta_part2 * n.cdf(-d2)
    };

    // Vanna: dDelta / dSigma
    let vanna = -n_prime_d1 * (d2 / sigma);

    // Charm: dDelta / dT
    let charm_part1 = n_prime_d1 * ( (2.0 * r * t - d2 * sigma * t.sqrt()) / (2.0 * t * sigma * t.sqrt()) );
    let charm = if is_call {
        -charm_part1
    } else {
        charm_part1
    };

    Greeks { delta, gamma, vega, theta, vanna, charm }
}
