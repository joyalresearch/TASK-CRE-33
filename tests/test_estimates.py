
import unittest

from src.iris_cre_33.estimates import estimate_pv_kwp


class TestPVEstimates(unittest.TestCase):

    def test_known_pv_estimate(self):
        result = estimate_pv_kwp(
            footprint_area_sqm=1000,
            usable_fraction=0.60,
            sqm_per_kwp=5.0,
        )

        self.assertAlmostEqual(
            result["usable_roof_area_sqm"], 600
        )
        self.assertAlmostEqual(
            result["estimated_pv_kwp"], 120
        )

    def test_zero_area_rejected(self):
        with self.assertRaises(ValueError):
            estimate_pv_kwp(0)

    def test_invalid_usable_fraction_rejected(self):
        with self.assertRaises(ValueError):
            estimate_pv_kwp(1000, usable_fraction=1.5)

    def test_invalid_panel_density_rejected(self):
        with self.assertRaises(ValueError):
            estimate_pv_kwp(1000, sqm_per_kwp=0)


if __name__ == "__main__":
    unittest.main()
