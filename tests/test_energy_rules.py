import unittest

from energy_rules import evaluate_energy_state


class EvaluateEnergyStateTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "comfort_min_c": 20.0,
            "comfort_max_c": 26.0,
            "critical_temp_c": 30.0,
        }

    def test_hot_occupied_room_requests_fan_auto_command(self):
        result = evaluate_energy_state(
            {
                "temp_c": 28.2,
                "presence": True,
                "fan_on": False,
                "lamp_on": False,
                "window_open": False,
                "power_w": 180.0,
                "temp_ok": True,
                "anomaly_dht_fail": False,
            },
            self.profile,
        )

        self.assertEqual(result["thermal_state"], "trop_chaud")
        self.assertEqual(result["recommended_action"], "fan_on")
        self.assertEqual(result["auto_command"], "fan_on")
        self.assertFalse(result["critical_temp_alert"])
        self.assertEqual(result["energy_anomaly"], [])
        self.assertLess(result["comfort_score"], 100)
        self.assertTrue(result["reasons"])

    def test_absence_and_waste_create_anomalies_without_auto_command(self):
        result = evaluate_energy_state(
            {
                "temp_c": 31.0,
                "presence": False,
                "fan_on": True,
                "lamp_on": True,
                "window_open": True,
                "power_w": 3200.0,
                "temp_ok": False,
                "anomaly_dht_fail": True,
            },
            self.profile,
        )

        self.assertEqual(result["thermal_state"], "trop_chaud")
        self.assertEqual(result["recommended_action"], "fan_on")
        self.assertIsNone(result["auto_command"])
        self.assertTrue(result["critical_temp_alert"])
        self.assertIn("waste_lighting_absence", result["energy_anomaly"])
        self.assertIn("waste_ventilation_absence", result["energy_anomaly"])
        self.assertIn("open_window_ventilation_loss", result["energy_anomaly"])
        self.assertIn("overpower", result["energy_anomaly"])
        self.assertLessEqual(result["comfort_score"], 40)
        self.assertTrue(any("capteur" in reason.lower() for reason in result["reasons"]))

    def test_cold_room_recommends_heating_alert(self):
        result = evaluate_energy_state(
            {
                "temp_c": 18.0,
                "presence": True,
                "fan_on": False,
                "lamp_on": False,
                "window_open": False,
                "power_w": 120.0,
                "temp_ok": True,
                "anomaly_dht_fail": False,
            },
            self.profile,
        )

        self.assertEqual(result["thermal_state"], "trop_froid")
        self.assertEqual(result["recommended_action"], "heating_alert")
        self.assertIsNone(result["auto_command"])
        self.assertFalse(result["critical_temp_alert"])
        self.assertGreaterEqual(result["comfort_score"], 0)


if __name__ == "__main__":
    unittest.main()
