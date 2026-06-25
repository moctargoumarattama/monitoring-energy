import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import app as app_module


API_HEADERS = {
    "Content-Type": "application/json",
    "X-API-TOKEN": app_module.APP_TOKEN,
}

DEFAULT_INGEST_PAYLOAD = {
    "device_id": "esp32-1",
    "mode": "auto",
    "presence": True,
    "window_open": False,
    "temp_ok": True,
    "temp_c": 24.0,
    "humidity": 48.0,
    "current_a": 0.8,
    "power_w": 120.0,
    "energy_total_kwh": 999.0,
    "energy_before_kwh": 999.0,
    "energy_after_kwh": 999.0,
    "cost_mad": 999.0,
    "lamp_on": False,
    "fan_on": False,
    "remote_enabled": True,
    "anomaly_dht_fail": False,
}


class ComfortProfileApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.temp_dir.name, "test_energy.db")
        app_module.DB_PATH = self.db_path
        app_module.app.config["TESTING"] = True
        with app_module.app.app_context():
            app_module.init_db()
        self.client = app_module.app.test_client()

    def tearDown(self):
        with app_module.app.app_context():
            app_module.close_db(None)
        self.client = None
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def get_db_row(self, query, params=()):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(query, params).fetchone()

    def post_json_at(self, ts_value, path, payload, headers=API_HEADERS):
        with mock.patch.object(app_module.time, "time", return_value=ts_value):
            return self.client.post(path, headers=headers, json=payload)

    def ingest_at(self, ts_value, **overrides):
        payload = dict(DEFAULT_INGEST_PAYLOAD)
        payload.update(overrides)
        return self.post_json_at(ts_value, "/api/ingest", payload)

    def test_set_and_get_comfort_profile(self):
        response = self.client.post(
            "/api/set_comfort_profile",
            headers=API_HEADERS,
            json={
                "comfort_min_c": 21,
                "comfort_max_c": 25,
                "critical_temp_c": 29,
            },
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/get_comfort_profile")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["profile"]["comfort_min_c"], 21.0)
        self.assertEqual(data["profile"]["comfort_max_c"], 25.0)
        self.assertEqual(data["profile"]["critical_temp_c"], 29.0)

    def test_ingest_persists_rule_outputs_and_queues_single_auto_command(self):
        self.client.post(
            "/api/set_comfort_profile",
            headers=API_HEADERS,
            json={
                "comfort_min_c": 20,
                "comfort_max_c": 26,
                "critical_temp_c": 30,
            },
        )

        payload = {
            "device_id": "esp32-1",
            "mode": "auto",
            "presence": True,
            "window_open": False,
            "temp_ok": True,
            "temp_c": 28.5,
            "current_a": 0.9,
            "power_w": 200.0,
            "energy_total_kwh": 1.25,
            "energy_before_kwh": 0.4,
            "energy_after_kwh": 0.85,
            "cost_mad": 1.5,
            "lamp_on": False,
            "fan_on": False,
            "remote_enabled": True,
            "anomaly_dht_fail": False,
        }

        first = self.client.post("/api/ingest", headers=API_HEADERS, json=payload)
        second = self.client.post("/api/ingest", headers=API_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        latest = self.client.get("/api/latest?device_id=esp32-1")
        self.assertEqual(latest.status_code, 200)
        latest_data = latest.get_json()["data"]

        self.assertEqual(latest_data["thermal_state"], "trop_chaud")
        self.assertEqual(latest_data["recommended_action"], "fan_on")
        self.assertEqual(latest_data["auto_command"], "fan_on")
        self.assertNotIn("season", latest_data)

        command_count = self.get_db_row(
            "SELECT COUNT(*) AS n FROM commands WHERE device_id=? AND command='fan_on' AND source='auto'",
            ("esp32-1",),
        )["n"]
        self.assertEqual(command_count, 1)

    def test_recent_manual_fan_command_blocks_automatic_override(self):
        self.client.post(
            "/api/set_comfort_profile",
            headers=API_HEADERS,
            json={
                "comfort_min_c": 20,
                "comfort_max_c": 26,
                "critical_temp_c": 30,
            },
        )

        manual = self.client.post(
            "/api/cmd",
            headers=API_HEADERS,
            json={
                "device_id": "esp32-1",
                "command": "fan_off",
                "payload": {},
            },
        )
        self.assertEqual(manual.status_code, 200)

        ingest = self.client.post(
            "/api/ingest",
            headers=API_HEADERS,
            json={
                "device_id": "esp32-1",
                "mode": "auto",
                "presence": True,
                "window_open": False,
                "temp_ok": True,
                "temp_c": 29.0,
                "current_a": 0.7,
                "power_w": 180.0,
                "energy_total_kwh": 1.5,
                "energy_before_kwh": 0.5,
                "energy_after_kwh": 1.0,
                "cost_mad": 1.8,
                "lamp_on": False,
                "fan_on": False,
                "remote_enabled": True,
                "anomaly_dht_fail": False,
            },
        )
        self.assertEqual(ingest.status_code, 200)

        auto_count = self.get_db_row(
            "SELECT COUNT(*) AS n FROM commands WHERE device_id=? AND source='auto'",
            ("esp32-1",),
        )["n"]
        self.assertEqual(auto_count, 0)

    def test_season_commands_are_rejected(self):
        response = self.client.post(
            "/api/cmd",
            headers=API_HEADERS,
            json={
                "device_id": "esp32-1",
                "command": "season_hiver",
                "payload": {},
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_legacy_mode_commands_are_rejected(self):
        for command in ("mode_avant", "mode_apres"):
            response = self.client.post(
                "/api/cmd",
                headers=API_HEADERS,
                json={
                    "device_id": "esp32-1",
                    "command": command,
                    "payload": {},
                },
            )
            self.assertEqual(response.status_code, 400)

    def test_window_commands_are_accepted(self):
        for command in ("window_open", "window_close"):
            response = self.client.post(
                "/api/cmd",
                headers=API_HEADERS,
                json={
                    "device_id": "esp32-1",
                    "command": command,
                    "payload": {},
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["queued"])
            row = self.get_db_row(
                "SELECT command, status FROM commands WHERE id=?",
                (payload["id"],),
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["command"], command)
            self.assertEqual(row["status"], "pending")

    def test_ingest_calculates_backend_energy_and_cost_from_power_and_persisted_price(self):
        set_price = self.post_json_at(
            100,
            "/api/cmd",
            {
                "device_id": "esp32-1",
                "command": "set_price",
                "payload": {"price": 2.0},
            },
        )
        self.assertEqual(set_price.status_code, 200)

        first = self.ingest_at(1000, power_w=100.0, current_a=0.5)
        second = self.ingest_at(4600, power_w=100.0, current_a=0.5)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        latest = self.client.get("/api/latest?device_id=esp32-1")
        self.assertEqual(latest.status_code, 200)
        latest_data = latest.get_json()["data"]

        self.assertAlmostEqual(latest_data["energy_total_kwh"], 0.1, places=4)
        self.assertAlmostEqual(latest_data["cost_mad"], 0.2, places=4)

        price_row = self.get_db_row(
            "SELECT value FROM app_settings WHERE key='price_per_kwh'"
        )
        self.assertIsNotNone(price_row)
        self.assertAlmostEqual(float(price_row["value"]), 2.0, places=4)

    def test_study_routes_track_before_after_and_result(self):
        set_price = self.post_json_at(
            100,
            "/api/cmd",
            {
                "device_id": "esp32-1",
                "command": "set_price",
                "payload": {"price": 2.0},
            },
        )
        self.assertEqual(set_price.status_code, 200)

        self.assertEqual(self.ingest_at(1000, power_w=100.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(1000, "/api/study/start_before", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(4600, power_w=100.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(4600, "/api/study/stop_before", {"device_id": "esp32-1"}).status_code,
            200,
        )

        self.assertEqual(self.ingest_at(4601, power_w=60.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(4601, "/api/study/start_after", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(8201, power_w=60.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(8201, "/api/study/stop_after", {"device_id": "esp32-1"}).status_code,
            200,
        )

        result = self.client.get("/api/study/result?device_id=esp32-1")
        self.assertEqual(result.status_code, 200)
        payload = result.get_json()

        self.assertAlmostEqual(payload["before_kwh"], 0.1, places=4)
        self.assertAlmostEqual(payload["after_kwh"], 0.06, places=4)
        self.assertAlmostEqual(payload["gain_kwh"], 0.04, places=4)
        self.assertAlmostEqual(payload["reduction_percent"], 40.0, places=2)
        self.assertAlmostEqual(payload["cost_saving"], 0.08, places=4)
        self.assertEqual(payload["duration_before"], 3600)
        self.assertEqual(payload["duration_after"], 3600)

        latest_study = self.client.get("/api/latest_study?device_id=esp32-1")
        self.assertEqual(latest_study.status_code, 200)
        latest_study_data = latest_study.get_json()["data"]
        self.assertIsNotNone(latest_study_data)
        self.assertAlmostEqual(latest_study_data["before_kwh"], 0.1, places=4)
        self.assertAlmostEqual(latest_study_data["after_kwh"], 0.06, places=4)

    def test_study_result_is_normalized_when_after_exceeds_before(self):
        self.assertEqual(self.ingest_at(1000, power_w=100.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(1000, "/api/study/start_before", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(4600, power_w=100.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(4600, "/api/study/stop_before", {"device_id": "esp32-1"}).status_code,
            200,
        )

        self.assertEqual(self.ingest_at(4601, power_w=200.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(4601, "/api/study/start_after", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(8201, power_w=200.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(8201, "/api/study/stop_after", {"device_id": "esp32-1"}).status_code,
            200,
        )

        result = self.client.get("/api/study/result?device_id=esp32-1")
        self.assertEqual(result.status_code, 200)
        payload = result.get_json()

        self.assertGreater(payload["before_kwh"], payload["after_kwh"])
        self.assertGreater(payload["gain_kwh"], 0.0)
        self.assertGreater(payload["reduction_percent"], 0.0)
        self.assertGreater(payload["cost_saving"], 0.0)

    def test_reset_study_command_resets_new_study_workflow_result(self):
        self.assertEqual(self.ingest_at(1000, power_w=100.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(1000, "/api/study/start_before", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(4600, power_w=100.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(4600, "/api/study/stop_before", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(4601, power_w=60.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(4601, "/api/study/start_after", {"device_id": "esp32-1"}).status_code,
            200,
        )
        self.assertEqual(self.ingest_at(8201, power_w=60.0).status_code, 200)
        self.assertEqual(
            self.post_json_at(8201, "/api/study/stop_after", {"device_id": "esp32-1"}).status_code,
            200,
        )

        before_reset = self.client.get("/api/study/result?device_id=esp32-1").get_json()
        self.assertGreater(before_reset["before_kwh"], 0.0)
        self.assertGreater(before_reset["after_kwh"], 0.0)

        reset_response = self.post_json_at(
            8300,
            "/api/cmd",
            {
                "device_id": "esp32-1",
                "command": "reset_study",
                "payload": {},
            },
        )
        self.assertEqual(reset_response.status_code, 200)

        after_reset = self.client.get("/api/study/result?device_id=esp32-1").get_json()
        self.assertEqual(after_reset["before_kwh"], 0.0)
        self.assertEqual(after_reset["after_kwh"], 0.0)
        self.assertEqual(after_reset["gain_kwh"], 0.0)
        self.assertEqual(after_reset["reduction_percent"], 0.0)
        self.assertEqual(after_reset["cost_saving"], 0.0)
        self.assertEqual(after_reset["duration_before"], 0)
        self.assertEqual(after_reset["duration_after"], 0)

    def test_reset_energy_totals_command_resets_live_totals_display(self):
        self.assertEqual(self.ingest_at(1000, power_w=100.0).status_code, 200)
        self.assertEqual(self.ingest_at(4600, power_w=100.0).status_code, 200)

        latest_before = self.client.get("/api/latest?device_id=esp32-1")
        self.assertEqual(latest_before.status_code, 200)
        latest_before_data = latest_before.get_json()["data"]
        self.assertGreater(latest_before_data["energy_total_kwh"], 0.0)
        self.assertGreater(latest_before_data["cost_mad"], 0.0)

        reset_response = self.post_json_at(
            4700,
            "/api/cmd",
            {
                "device_id": "esp32-1",
                "command": "reset_energy_totals",
                "payload": {},
            },
        )
        self.assertEqual(reset_response.status_code, 200)
        reset_payload = reset_response.get_json()
        self.assertTrue(reset_payload["ok"])
        self.assertIn("backend", reset_payload)
        self.assertGreaterEqual(reset_payload["backend"]["energy_offset_kwh"], 0.0)

        offset_row = self.get_db_row(
            "SELECT value FROM app_settings WHERE key=?",
            ("energy_totals_reset_at:esp32-1",),
        )
        self.assertIsNotNone(offset_row)

        latest_after_reset = self.client.get("/api/latest?device_id=esp32-1")
        self.assertEqual(latest_after_reset.status_code, 200)
        latest_after_reset_data = latest_after_reset.get_json()["data"]
        self.assertEqual(latest_after_reset_data["energy_total_kwh"], 0.0)
        self.assertEqual(latest_after_reset_data["cost_mad"], 0.0)

        self.assertEqual(self.ingest_at(8201, power_w=100.0).status_code, 200)
        latest_after_restart = self.client.get("/api/latest?device_id=esp32-1")
        self.assertEqual(latest_after_restart.status_code, 200)
        latest_after_restart_data = latest_after_restart.get_json()["data"]
        self.assertGreater(latest_after_restart_data["energy_total_kwh"], 0.0)
        self.assertGreater(latest_after_restart_data["cost_mad"], 0.0)

    def test_cmd_ack_marks_pulled_command_as_acked(self):
        queued = self.post_json_at(
            5000,
            "/api/cmd",
            {
                "device_id": "esp32-1",
                "command": "fan_on",
                "payload": {},
            },
        )
        self.assertEqual(queued.status_code, 200)
        queued_data = queued.get_json()
        self.assertTrue(queued_data["ok"])
        self.assertIn("id", queued_data)

        with mock.patch.object(app_module.time, "time", return_value=5001):
            pulled = self.client.get(
                "/api/pull_cmd?device_id=esp32-1",
                headers={"X-API-TOKEN": app_module.APP_TOKEN},
            )
        self.assertEqual(pulled.status_code, 200)
        pulled_data = pulled.get_json()
        self.assertTrue(pulled_data["ok"])
        self.assertIsNotNone(pulled_data["cmd"])
        self.assertEqual(pulled_data["cmd"]["id"], queued_data["id"])

        ack = self.post_json_at(
            5002,
            "/api/cmd_ack",
            {
                "id": queued_data["id"],
                "status": "acked",
            },
        )
        self.assertEqual(ack.status_code, 200)
        self.assertTrue(ack.get_json()["ok"])

        status_row = self.get_db_row(
            "SELECT status FROM commands WHERE id=?",
            (queued_data["id"],),
        )
        self.assertIsNotNone(status_row)
        self.assertEqual(status_row["status"], "acked")

    def test_prediction_endpoint_projects_recent_average_power(self):
        self.assertEqual(self.ingest_at(1000, power_w=100.0).status_code, 200)
        self.assertEqual(self.ingest_at(1010, power_w=200.0).status_code, 200)
        self.assertEqual(self.ingest_at(1020, power_w=300.0).status_code, 200)

        response = self.client.get("/api/prediction?device_id=esp32-1")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertAlmostEqual(data["avg_power_w"], 200.0, places=4)
        self.assertAlmostEqual(data["predicted_kwh_1h"], 0.2, places=4)
        self.assertAlmostEqual(data["predicted_kwh_24h"], 4.8, places=4)
        self.assertAlmostEqual(data["predicted_cost_24h"], 5.76, places=4)
        self.assertEqual(data["risk_level"], "normal")

    def test_history_endpoint_returns_sorted_backend_telemetry(self):
        self.assertEqual(self.ingest_at(1000, power_w=100.0, temp_c=23.0).status_code, 200)
        self.assertEqual(self.ingest_at(4600, power_w=100.0, temp_c=24.5).status_code, 200)

        response = self.client.get("/api/history?device_id=esp32-1&limit=2")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 2)
        self.assertGreater(data["items"][0]["ts"], data["items"][1]["ts"])

        latest = data["items"][0]
        self.assertEqual(latest["temp_c"], 24.5)
        self.assertAlmostEqual(latest["humidity"], 48.0, places=4)
        self.assertAlmostEqual(latest["power_w"], 100.0, places=4)
        self.assertAlmostEqual(latest["energy_total_kwh"], 0.1, places=4)
        self.assertAlmostEqual(latest["cost_mad"], 0.12, places=4)
        self.assertIn("thermal_state", latest)
        self.assertIn("comfort_score", latest)
        self.assertIn("energy_anomaly_json", latest)

    def test_ai_anomaly_summary_uses_active_flags_for_primary_count(self):
        self.assertEqual(self.ingest_at(1000, anomaly_dht_fail=False, temp_ok=True, temp_c=24.0).status_code, 200)
        self.assertEqual(self.ingest_at(1010, anomaly_dht_fail=True, temp_ok=False, temp_c=0.0).status_code, 200)
        self.assertEqual(self.ingest_at(1020, anomaly_dht_fail=True, temp_ok=False, temp_c=0.0).status_code, 200)

        with mock.patch.object(app_module.time, "time", return_value=1030):
            response = self.client.get("/api/ai/anomaly_summary?device_id=esp32-1&minutes=60")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertEqual(data["anomalies"]["total"], 1)
        self.assertEqual(data["anomalies"]["overpower"], 0)
        self.assertEqual(data["anomalies"]["temp"], 0)
        self.assertEqual(data["anomalies"]["dht_fail"], 1)
        self.assertEqual(data["anomalies"]["window_total"], 2)
        self.assertEqual(data["anomalies"]["window_overpower"], 0)
        self.assertEqual(data["anomalies"]["window_temp"], 0)
        self.assertEqual(data["anomalies"]["window_dht_fail"], 2)

    def test_ai_anomaly_summary_keeps_history_without_marking_recovered_issue_active(self):
        self.assertEqual(self.ingest_at(1000, anomaly_dht_fail=True, temp_ok=False, temp_c=0.0).status_code, 200)
        self.assertEqual(self.ingest_at(1010, anomaly_dht_fail=True, temp_ok=False, temp_c=0.0).status_code, 200)
        self.assertEqual(self.ingest_at(1020, anomaly_dht_fail=False, temp_ok=True, temp_c=24.0).status_code, 200)

        with mock.patch.object(app_module.time, "time", return_value=1030):
            response = self.client.get("/api/ai/anomaly_summary?device_id=esp32-1&minutes=60")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertEqual(data["anomalies"]["total"], 0)
        self.assertEqual(data["anomalies"]["dht_fail"], 0)
        self.assertEqual(data["anomalies"]["window_total"], 2)
        self.assertEqual(data["anomalies"]["window_dht_fail"], 2)

    def test_dashboard_template_uses_new_study_prediction_and_history_routes(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertNotIn("MODE AVANT", html)
        self.assertNotIn("MODE APRES", html)
        self.assertNotIn("requestFinancialAudit()", html)
        self.assertNotIn("loadFinancialHistory()", html)
        self.assertNotIn("/api/financial_audit", html)
        self.assertNotIn("/api/financial_audits", html)
        self.assertNotIn("financeStudyBefore", html)
        self.assertNotIn("financeStudyAfter", html)
        self.assertNotIn("financeSavings", html)
        self.assertNotIn("AVANT:", html)
        self.assertNotIn("APRES:", html)
        self.assertNotIn("RESET ETUDE", html)
        self.assertIn("Commandes matérielles", html)
        self.assertIn("Paramètres acquisition", html)
        self.assertIn("Audit énergétique avant/après", html)
        self.assertIn("Réinitialiser audit", html)
        self.assertIn("D\u00e9marrer AVANT", html)
        self.assertIn("Stop AVANT", html)
        self.assertIn("D\u00e9marrer APR\u00c8S", html)
        self.assertIn("Stop APR\u00c8S", html)
        self.assertIn("Actualiser r\u00e9sultat", html)
        self.assertNotIn("Mode audit \u00e9nerg\u00e9tique actif - une seule cha\u00eene officielle bas\u00e9e sur les routes /api/study/*.", html)
        self.assertIn("studySavingsMad", html)
        self.assertIn("studyVisualGain", html)
        self.assertNotIn("studySavingsPercent", html)
        self.assertNotIn("studyCostSavingValue", html)
        self.assertNotIn('<span class="badge">IoT Energy Monitoring</span>', html)
        self.assertNotIn('<span class="badge">SCADA moderne</span>', html)
        self.assertIn("insightCard", html)
        self.assertIn("insightBadge", html)
        self.assertIn("insightRecommendationRow", html)
        self.assertIn("comfortCurrentStateKpi", html)
        self.assertNotIn("comfortCurrentActionKpi", html)
        self.assertLess(html.index('id="comfortCurrentStateKpi"'), html.index('id="comfortMin"'))
        self.assertNotIn("DAILY REPORT NOW", html)
        self.assertIn("/api/study/start_before", html)
        self.assertIn("/api/study/stop_before", html)
        self.assertIn("/api/study/start_after", html)
        self.assertIn("/api/study/stop_after", html)
        self.assertIn("/api/study/result", html)
        self.assertIn("reset_energy_totals", html)
        self.assertIn("/api/prediction", html)
        self.assertIn("/api/history", html)
        self.assertIn("cmd('window_open')", html)
        self.assertIn("cmd('window_close')", html)

    def test_dashboard_template_uses_clear_score_labels(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertNotIn("Score instantan\u00e9", html)
        self.assertNotIn("Bas\u00e9 sur la mesure actuelle", html)
        self.assertIn("Donn\u00e9es capt\u00e9es maintenant", html)
        self.assertIn("iotPresenceNow", html)
        self.assertIn("iotWindowNow", html)
        self.assertIn("iotHumidityNow", html)
        self.assertNotIn('id="comfortCurrentTemp"', html)
        self.assertNotIn("Temp\u00e9rature actuelle :", html)
        self.assertIn("Score global", html)
        self.assertIn("Tient compte du contexte", html)
        self.assertIn("Fiabilit\u00e9 des mesures", html)
        self.assertIn("Qualit\u00e9 des donn\u00e9es re\u00e7ues", html)
        self.assertIn("async function fetchLatest()", html)
        self.assertIn("async function fetchPrediction()", html)
        self.assertIn("commandPopup", html)
        self.assertIn("showCommandPopup", html)
        self.assertIn("playCommandSound", html)
        self.assertIn("getButtonFeedbackLabel", html)

    def test_dashboard_template_uses_active_anomaly_copy(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Anomalies actives", html)
        self.assertIn("Mesures concern\u00e9es sur 60 min", html)
        self.assertIn("Capteur temp\u00e9rature en d\u00e9faut", html)
        self.assertIn("Surconsommation active", html)
        self.assertIn("Temp\u00e9rature critique", html)
        self.assertIn('class="atotal atotal-ok"', html)
        self.assertIn('class="tag ok">Aucune anomalie active</span>', html)
        self.assertIn("Syst\u00e8me normal", html)
        self.assertIn("anomalyState-ok", html)
        self.assertIn("anomalyState-alert", html)
        self.assertIn("atotal-ok", html)
        self.assertIn("atotal-alert", html)
        self.assertIn("Humidit", html)
        self.assertIn("iotHumidityNow", html)
        self.assertIn("async function updateDashboard()", html)
        self.assertIn("setInterval(updateDashboard, 1000)", html)

    def test_dashboard_prediction_rendering_does_not_depend_on_missing_cost_node(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertNotIn('document.getElementById("predCost24").textContent', html)
        self.assertIn('setNodeText("predPower"', html)
        self.assertIn('setNodeText("predEnergy"', html)
        self.assertIn('setNodeText("predEnergy24"', html)
        self.assertIn('setNodeText("predRisk"', html)
        self.assertIn("chart.update(", html)
        self.assertIn("connBadge", html)


if __name__ == "__main__":
    unittest.main()
