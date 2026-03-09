use std::collections::VecDeque;

pub struct OfiEngine {
    ofi_series: VecDeque<f64>,
}

impl OfiEngine {
    pub fn new() -> Self {
        OfiEngine {
            ofi_series: VecDeque::with_capacity(100),
        }
    }

    pub fn calculate_ofi_zscore(&mut self, ofi: f64) -> f64 {
        self.ofi_series.push_back(ofi);
        if self.ofi_series.len() > 100 {
            self.ofi_series.pop_front();
        }

        if self.ofi_series.len() < 20 {
            return 0.0;
        }

        let mean: f64 = self.ofi_series.iter().sum::<f64>() / self.ofi_series.len() as f64;
        let variance: f64 = self.ofi_series.iter()
            .map(|x| (x - mean).powi(2))
            .sum::<f64>() / self.ofi_series.len() as f64;
        
        let std_dev = variance.sqrt();
        if std_dev > 0.0 {
            (ofi - mean) / std_dev
        } else {
            0.0
        }
    }
}
