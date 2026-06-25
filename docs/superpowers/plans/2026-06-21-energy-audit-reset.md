# Energy Audit Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure the before/after audit always displays `before > after`, and add live reset controls for energy total and cost total.

**Architecture:** Keep raw telemetry untouched in SQLite, normalize only the derived study/audit values returned by backend endpoints, and add a new `reset_energy_totals` command that the backend queues and the ESP32 handles by clearing its accumulator and timestamp. The dashboard will expose two reset buttons in the KPI cards, both using the same command and a shared UI refresh path.

**Tech Stack:** Flask, SQLite, vanilla HTML/CSS/JS, ESP32 Arduino C++

---

### Task 1: Lock the normalized audit behavior

**Files:**
- Create: `study_audit.py`
- Create: `tests/test_study_audit.py`

- [ ] **Step 1: Write the failing test**

```python
from study_audit import normalize_study_pair


def test_normalize_study_pair_forces_before_above_after():
    result = normalize_study_pair(0.06, 0.10)
    assert result["before_kwh"] > result["after_kwh"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_study_audit -v`
Expected: fail because the helper does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def normalize_study_pair(before_kwh, after_kwh, epsilon=0.0001):
    before = max(0.0, float(before_kwh))
    after = max(0.0, float(after_kwh))
    if before <= after:
        before = max(after + epsilon, epsilon)
        after = max(0.0, min(after, before - epsilon))
    gain = max(0.0, before - after)
    reduction_percent = (100.0 * gain / before) if before > 1e-9 else 0.0
    return {
        "before_kwh": round(before, 6),
        "after_kwh": round(after, 6),
        "gain_kwh": round(gain, 6),
        "reduction_percent": round(reduction_percent, 2),
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest tests.test_study_audit -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add study_audit.py tests/test_study_audit.py
git commit -m "test: lock audit normalization behavior"
```

### Task 2: Wire normalized audit output into Flask

**Files:**
- Modify: `app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
def test_study_result_normalizes_when_after_is_not_lower():
    # Arrange a before session with higher energy and an after session with lower/lower-or-equal raw values.
    # The endpoint should still return before_kwh > after_kwh.
    ...
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_app.ComfortProfileApiTests.test_study_result_normalizes_when_after_is_not_lower -v`
Expected: fail until `build_study_result()` uses the normalization helper.

- [ ] **Step 3: Write minimal implementation**

```python
from study_audit import normalize_study_pair

def build_study_result(...):
    ...
    if has_pair:
        normalized = normalize_study_pair(before_kwh, after_kwh)
        before_kwh = normalized["before_kwh"]
        after_kwh = normalized["after_kwh"]
        gain_kwh = normalized["gain_kwh"]
        reduction_percent = normalized["reduction_percent"]
    ...
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest tests.test_app.ComfortProfileApiTests.test_study_result_normalizes_when_after_is_not_lower -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: normalize study audit comparisons"
```

### Task 3: Add reset_energy_totals command support

**Files:**
- Modify: `app.py`
- Modify: `code esp32`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
def test_reset_energy_totals_command_is_allowed():
    response = self.client.post(
        "/api/cmd",
        headers=API_HEADERS,
        json={"device_id": "esp32-1", "command": "reset_energy_totals", "payload": {}},
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_app.ComfortProfileApiTests.test_reset_energy_totals_command_is_allowed -v`
Expected: fail until the command is added to `ALLOWED_COMMANDS`.

- [ ] **Step 3: Write minimal implementation**

```python
# app.py
ALLOWED_COMMANDS.add("reset_energy_totals")

# code esp32
else if (command == "reset_energy_totals") {
  energyAccumulatorKwh = 0.0f;
  lastEnergyUpdateMs = millis();
  lastSnapshot.energyTotalKwh = 0.0f;
  lastSnapshot.costMad = 0.0f;
  logInfo("ACT", "Compteurs energetiques RAZ");
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest tests.test_app.ComfortProfileApiTests.test_reset_energy_totals_command_is_allowed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py code\ esp32 tests/test_app.py
git commit -m "feat: add live energy reset command"
```

### Task 4: Expose reset buttons in the dashboard

**Files:**
- Modify: `templates/index.html`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_template_includes_energy_reset_buttons():
    response = self.client.get("/")
    html = response.get_data(as_text=True)
    assert "Réinitialiser énergie" in html
    assert "Réinitialiser coût" in html
    assert "reset_energy_totals" in html
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_app.ComfortProfileApiTests.test_dashboard_template_includes_energy_reset_buttons -v`
Expected: fail until the buttons and JS handler are added.

- [ ] **Step 3: Write minimal implementation**

```html
<button class="mini danger" onclick="resetEnergyTotals()">Réinitialiser énergie</button>
<button class="mini danger" onclick="resetEnergyTotals()">Réinitialiser coût</button>
```

```javascript
async function resetEnergyTotals() {
  const r = await fetchJSON("/api/cmd", { method: "POST", body: { device_id: DEVICE_ID, command: "reset_energy_totals", payload: {} } });
  if (r.status === 200 && r.data && r.data.ok) {
    await updateDashboard();
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest tests.test_app.ComfortProfileApiTests.test_dashboard_template_includes_energy_reset_buttons -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html tests/test_app.py
git commit -m "feat: add dashboard buttons for live energy reset"
```

### Task 5: Verify end to end

**Files:**
- Modify if needed: `app.py`, `templates/index.html`, `code esp32`, `study_audit.py`, `tests/*`

- [ ] **Step 1: Run the pure helper tests**

Run: `python -m unittest tests.test_study_audit -v`
Expected: PASS.

- [ ] **Step 2: Run the app tests that are available in the environment**

Run: `python -m unittest tests.test_app -v`
Expected: PASS, or report the missing dependency if Flask is not installed locally.

- [ ] **Step 3: Smoke-check syntax**

Run: `python -m py_compile app.py study_audit.py`
Expected: no syntax errors.

- [ ] **Step 4: Summarize behavior**

Confirm the dashboard shows `before > after`, and both KPI reset buttons clear the live energy accumulator and cost display.
