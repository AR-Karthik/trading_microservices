# K.A.R.T.H.I.K. Pre-Market Operational Checklist (09:00 - 09:15 IST)

This document defines the mandatory manual verification steps for the Strategist before the market open. These steps ensure the automated "Fortress" is aligned with the day's risk objectives.

---

## 🟢 09:00 - 09:05 IST: Automated Pre-Flight Review

- [ ] **Acknowledge Telegram Pre-Flight Report**:
    - Verify status is `✅ SUCCESS: ALL KNIGHTS ARMED`.
    - Confirm `LIVE_CAPITAL_LIMIT` matches the intended funding for the session.
    - Confirm `STOP_DAY_LOSS` matches the predefined risk budget (e.g., 2.0%).
- [ ] **Authentication Audit**:
    - Ensure Shoonya API connection is active.
    - If the report shows `AUTH_FAILURE`, manually trigger `./scripts/reauth_shoonya.sh`.

## 🟡 09:05 - 09:10 IST: Infrastructure & Performance Sync

- [ ] **UI Dashboard Verification**:
    - Navigate to the **System Health** tab.
    - Confirm all 8 daemons are pulsing (Heartbeat < 1000ms).
    - Confirm **CPU Affinity**: Ensure `market_sensor` is pinned to high-performance cores.
- [ ] **Shared Memory (SHM) Integrity**:
    - Check the dashboard for `CRC_MISMATCH` alerts. If seen, restart the `tick_sensor` container.

## 🔵 09:10 - 09:14 IST: Strategic Context & Deterministic Alignment

- [ ] **Regime Analysis (Deterministic)**:
    - Review the **Deep Dive** tab for the current Regime State (S18).
    - If S18 = 3 (Volatility Spike), prepare for immediate positional risk reduction.
- [ ] **Signal Matrix Audit**:
    - Verify `S27` (Regime Quality) > 70.
    - If `S27` < 30, consider setting the system to `READ_ONLY` mode via the command center to prevent momentum traps.

## 🔴 09:14 IST: Final ARM

- [ ] **Atomic Arming**:
    - Confirm `SYSTEM_STATE = READY` in Redis/Dashboard.
    - Log into the Broker terminal to verify no stale "Phantom" orders exist.
- [ ] **Terminal Scope**:
    - Leave the `SystemController` terminal open to monitor the **09:15 IST** trade injection burst.

---
> [!IMPORTANT]
> Failure to complete this checklist before the 09:15 IST bell violates the **Zero-Trust Mandate**. If any step fails, the Strategist MUST hit the `/PANIC` button on Telegram.
