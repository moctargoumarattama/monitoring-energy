import unittest

from study_audit import normalize_study_pair


class NormalizeStudyPairTests(unittest.TestCase):
    def test_keeps_strictly_descending_pairs_unchanged(self):
        result = normalize_study_pair(0.123456, 0.045678)

        self.assertGreater(result["before_kwh"], result["after_kwh"])
        self.assertAlmostEqual(result["before_kwh"], 0.123456, places=6)
        self.assertAlmostEqual(result["after_kwh"], 0.045678, places=6)
        self.assertAlmostEqual(result["gain_kwh"], 0.077778, places=6)
        self.assertGreater(result["reduction_percent"], 0.0)

    def test_forces_before_above_after_when_after_is_not_lower(self):
        result = normalize_study_pair(0.05, 0.08)

        self.assertGreater(result["before_kwh"], result["after_kwh"])
        self.assertGreater(result["gain_kwh"], 0.0)
        self.assertGreater(result["reduction_percent"], 0.0)

    def test_zero_pair_gets_minimal_positive_gap(self):
        result = normalize_study_pair(0.0, 0.0)

        self.assertGreater(result["before_kwh"], result["after_kwh"])
        self.assertGreater(result["gain_kwh"], 0.0)
