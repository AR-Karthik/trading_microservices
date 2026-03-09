use std::collections::VecDeque;

pub struct VpinEngine {
    vpin_bucket_size: i64,
    current_bucket_vol: i64,
    current_bucket_buy_vol: i64,
    current_bucket_sell_vol: i64,
    vpin_series: VecDeque<f64>,
}

impl VpinEngine {
    pub fn new(vpin_bucket_size: i64) -> Self {
        VpinEngine {
            vpin_bucket_size,
            current_bucket_vol: 0,
            current_bucket_buy_vol: 0,
            current_bucket_sell_vol: 0,
            vpin_series: VecDeque::with_capacity(100),
        }
    }

    pub fn update(&mut self, sign: f64, volume: i64) -> Option<f64> {
        self.current_bucket_vol += volume;
        if sign > 0.0 {
            self.current_bucket_buy_vol += volume;
        } else if sign < 0.0 {
            self.current_bucket_sell_vol += volume;
        }

        if self.current_bucket_vol >= self.vpin_bucket_size {
            let vpin = (self.current_bucket_buy_vol - self.current_bucket_sell_vol).abs() as f64
                / self.current_bucket_vol as f64;
            self.vpin_series.push_back(vpin);
            if self.vpin_series.len() > 100 {
                self.vpin_series.pop_front();
            }
            self.current_bucket_vol = 0;
            self.current_bucket_buy_vol = 0;
            self.current_bucket_sell_vol = 0;
            Some(vpin)
        } else {
            None
        }
    }

    pub fn get_latest(&self) -> f64 {
        *self.vpin_series.back().unwrap_or(&0.0)
    }
}
