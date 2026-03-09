use pyo3::prelude::*;
use pyo3::types::PyDict;

mod modules;
use modules::ofi::OfiEngine;
use modules::vpin::VpinEngine;
use modules::greeks::calculate_greeks;

#[pyclass]
#[derive(Clone)]
pub struct TickData {
    #[pyo3(get, set)]
    pub price: f64,
    #[pyo3(get, set)]
    pub bid: f64,
    #[pyo3(get, set)]
    pub ask: f64,
    #[pyo3(get, set)]
    pub last_volume: i64,
}

#[pymethods]
impl TickData {
    #[new]
    fn new(price: f64, bid: f64, ask: f64, last_volume: i64) -> Self {
        TickData {
            price,
            bid,
            ask,
            last_volume,
        }
    }
}

#[pyclass]
pub struct TickEngine {
    vpin: VpinEngine,
    ofi: OfiEngine,
    cvd: f64,
}

#[pymethods]
impl TickEngine {
    #[new]
    fn new(vpin_bucket_size: i64) -> Self {
        TickEngine {
            vpin: VpinEngine::new(vpin_bucket_size),
            ofi: OfiEngine::new(),
            cvd: 0.0,
        }
    }

    pub fn classify_trade(&self, tick: &TickData) -> f64 {
        if tick.bid <= 0.0 || tick.ask <= 0.0 {
            return 0.0;
        }
        let mid = (tick.bid + tick.ask) / 2.0;
        if tick.price > mid {
            1.0
        } else if tick.price < mid {
            -1.0
        } else {
            0.0
        }
    }

    pub fn update_vpin(&mut self, tick: &TickData) -> Option<f64> {
        let sign = self.classify_trade(tick);
        self.vpin.update(sign, tick.last_volume)
    }

    pub fn get_latest_vpin(&self) -> f64 {
        self.vpin.get_latest()
    }

    pub fn update_ofi_zscore(&mut self, tick: &TickData) -> f64 {
        let sign = self.classify_trade(tick);
        let ofi = sign * tick.last_volume as f64;
        self.ofi.calculate_ofi_zscore(ofi)
    }

    pub fn update_cvd(&mut self, tick: &TickData) -> f64 {
        let sign = self.classify_trade(tick);
        let volume = tick.last_volume as f64;
        self.cvd += sign * volume;
        self.cvd
    }

    pub fn get_greeks(&self, py: Python, s: f64, k: f64, t: f64, r: f64, sigma: f64, is_call: bool) -> PyResult<PyObject> {
        let res = calculate_greeks(s, k, t, r, sigma, is_call);
        let dict = PyDict::new_bound(py);
        dict.set_item("delta", res.delta)?;
        dict.set_item("gamma", res.gamma)?;
        dict.set_item("vega", res.vega)?;
        dict.set_item("theta", res.theta)?;
        dict.set_item("vanna", res.vanna)?;
        dict.set_item("charm", res.charm)?;
        Ok(dict.to_object(py))
    }
}

#[pymodule]
fn tick_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<TickData>()?;
    m.add_class::<TickEngine>()?;
    Ok(())
}
