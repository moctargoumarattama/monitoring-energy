# Comfort Zone Energy Rules Design

**Date:** 2026-04-29

## Goal

Replace all season-based thermal behavior with a centralized comfort-zone rule engine driven by backend-owned configuration and persisted audit results.

## Scope

- Remove active use of `season`, `season_hiver`, and `season_ete`
- Add a root-level `energy_rules.py` module that evaluates thermal state, recommendations, anomalies, scoring, and auto-actions
- Add backend comfort-profile storage and APIs
- Store evaluated business outputs directly in `telemetry`
- Update the dashboard to configure and display the comfort-zone model
- Preserve existing manual control buttons and the rest of the monitoring flows

## Architecture

### Rule Engine

`energy_rules.py` owns the business evaluation entry point:

```python
evaluate_energy_state(data, comfort_profile)
```

The function receives normalized ESP32 telemetry plus the active comfort profile and returns:

- `thermal_state`
- `recommended_action`
- `critical_temp_alert`
- `energy_anomaly`
- `comfort_score`
- `auto_command`
- `reasons`

The module stays pure: it does not talk to Flask, SQLite, or command queues.

### Backend Ownership

`app.py` remains the orchestration layer:

1. parse and normalize ingest payload
2. load the active comfort profile
3. call `evaluate_energy_state(...)`
4. resolve whether the returned `auto_command` may actually be queued
5. persist raw telemetry plus evaluated outputs
6. expose the stored results through the existing read endpoints

This keeps the backend as the source of truth while keeping the rules testable in isolation.

## Data Model

### `telemetry`

The new schema removes all season fields and stores these backend-evaluated fields directly:

- `thermal_state`
- `recommended_action`
- `critical_temp_alert`
- `energy_anomaly_json`
- `comfort_score`
- `auto_command`
- `reasons_json`

Existing raw telemetry fields such as `temp_c`, `presence`, `fan_on`, `lamp_on`, `window_open`, and power/energy values remain.

### `comfort_profiles`

Single active profile persisted in SQLite:

- `id`
- `comfort_min_c`
- `comfort_max_c`
- `critical_temp_c`
- `updated_at`

The application keeps a single active row with sensible defaults.

### `commands`

Manual and automatic commands share the same queue. A `source` field distinguishes `manual` from `auto` so the backend can respect recent manual overrides when deciding whether an automatic command is still safe to send.

## Business Rules

### Thermal Comfort

- `temp_c > comfort_max_c` -> `thermal_state = "trop_chaud"`
- `comfort_min_c <= temp_c <= comfort_max_c` -> `thermal_state = "confort"`
- `temp_c < comfort_min_c` -> `thermal_state = "trop_froid"`
- `temp_c > critical_temp_c` -> `critical_temp_alert = true`

### Hybrid Action Logic

- If presence is detected, temperature is above `comfort_max_c`, and the fan is off:
  - `recommended_action = "fan_on"`
  - `auto_command = "fan_on"`
- If no presence is detected:
  - no automatic comfort command is sent
- If the space is too cold:
  - `recommended_action = "heating_alert"`
  - no automatic command is sent

### Energy Audit

Detected anomalies include:

- `waste_lighting_absence`
- `waste_ventilation_absence`
- `overpower`
- `open_window_ventilation_loss`

Sensor failure is still tracked from raw telemetry and contributes to score penalties and dashboard visibility.

### Comfort Score

Start at `100` and subtract penalties for:

- thermal discomfort
- critical temperature alert
- waste while absent
- overpower
- open-window ventilation loss
- sensor failure

The final score is clamped between `0` and `100`.

## Automatic Command Safety

Automatic comfort commands are allowed only when:

- the rule engine requests an `auto_command`
- the current telemetry indicates the target state is not already achieved
- there is no recent pending or sent equivalent auto-command
- there is no recent manual fan control command still within a backend override cooldown window

This keeps manual control authoritative and prevents repeated `fan_on` spam.

## API Changes

### New

- `GET /api/get_comfort_profile`
- `POST /api/set_comfort_profile`

### Updated

- `POST /api/ingest` now evaluates and stores rule-engine outputs
- `GET /api/latest` returns the new telemetry outputs for dashboard rendering
- command validation removes `season_hiver` and `season_ete`

## Frontend Changes

The dashboard adds a new “Zone de confort thermique” block with:

- minimum temperature input
- maximum temperature input
- critical temperature input
- apply button
- current temperature summary
- current thermal state
- recommended action

Season display and season buttons are removed. Manual lamp/fan buttons remain.

## Testing Strategy

- unit tests for `evaluate_energy_state(...)`
- Flask tests for comfort-profile APIs
- Flask ingest tests covering:
  - rule evaluation persistence
  - auto-command queueing
  - anti-spam behavior
  - manual override precedence

## Notes

- The workspace is not a Git repository, so this spec can be written locally but not committed here.
