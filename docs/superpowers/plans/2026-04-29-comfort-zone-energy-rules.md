# Comfort Zone Energy Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace season-based thermal behavior with a backend-owned comfort-zone rule engine and updated dashboard controls.

**Architecture:** Keep Flask orchestration in `app.py`, extract business evaluation into `energy_rules.py`, persist comfort-profile configuration in SQLite, and render the stored results in the dashboard. Preserve the existing command queue and manual controls while adding safe automatic fan activation.

**Tech Stack:** Flask, SQLite, vanilla HTML/CSS/JS, Python `unittest`

---

### Task 1: Lock behavior with tests

**Files:**
- Create: `tests/test_energy_rules.py`
- Create: `tests/test_app.py`

- [ ] Write failing unit tests for comfort, anomaly, and scoring rules
- [ ] Write failing Flask tests for comfort-profile APIs and `/api/ingest`
- [ ] Run: `python -m unittest discover -s tests -v`
- [ ] Confirm failures reference missing rule engine behavior and missing comfort-profile support

### Task 2: Add the rule engine

**Files:**
- Create: `energy_rules.py`

- [ ] Implement `evaluate_energy_state(data, comfort_profile)`
- [ ] Encode thermal-state, recommendation, anomaly, score, and auto-command rules
- [ ] Keep the module pure and independent from Flask/SQLite
- [ ] Re-run targeted tests for the rule engine

### Task 3: Refactor backend schema and ingest flow

**Files:**
- Modify: `app.py`

- [ ] Replace telemetry schema creation with the new season-free schema
- [ ] Add comfort-profile persistence helpers and default profile bootstrapping
- [ ] Add comfort-profile API routes
- [ ] Refactor command queueing into reusable helpers with `source` support
- [ ] Integrate rule evaluation into `/api/ingest`
- [ ] Persist evaluated fields and resolve automatic command suppression safely
- [ ] Remove season commands from validation/compression logic

### Task 4: Update read paths and anomaly summaries

**Files:**
- Modify: `app.py`

- [ ] Update `/api/latest` serialization for JSON-backed anomaly/reason fields
- [ ] Update aggregated daily and anomaly summary queries to use `comfort_score` and the new anomaly model
- [ ] Keep existing consumers working without season fields

### Task 5: Update the dashboard

**Files:**
- Modify: `templates/index.html`

- [ ] Remove active season display and buttons
- [ ] Add the comfort-profile configuration card
- [ ] Render thermal state, recommended action, and anomalies from backend data
- [ ] Load and submit the comfort profile through the new API
- [ ] Keep manual control buttons unchanged

### Task 6: Verify end to end

**Files:**
- Modify if needed: `app.py`, `templates/index.html`, `energy_rules.py`, `tests/*`

- [ ] Run: `python -m unittest discover -s tests -v`
- [ ] Fix any regressions discovered during verification
- [ ] Smoke-check the Flask app imports cleanly
- [ ] Summarize assumptions and remaining ESP32-side follow-up if those files are added later

## Self-Review

- Spec coverage: schema, rules engine, comfort-profile APIs, ingest integration, anti-spam, frontend block, and season removal are all covered above.
- Placeholder scan: no TODO/TBD placeholders remain.
- Type consistency: plan assumes `energy_anomaly_json` and `reasons_json` are JSON strings persisted in SQLite and decoded for API responses.
