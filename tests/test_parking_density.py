"""Check source-adapted parking density and historical consistency."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import test_parking_pv as historical


PROJECT_ROOT = Path(__file__).resolve().parents[1]

CURRENT_REPORT = PROJECT_ROOT / (
    "data/processed/parking_density_review/"
    "parking_pv_scenarios_05788ba5707c4d3d80e0085ea8cbcc92.json"
)

PV_REPORT = PROJECT_ROOT / (
    "data/processed/"
    "reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json"
)


class TestParkingDensity(historical.TestParkingPV):
    """Apply the existing geometry/identity checks to the new report."""

    @classmethod
    def setUpClass(cls):
        with patch.object(historical, "REPORT_PATH", CURRENT_REPORT):
            super().setUpClass()

    def run_estimator(self, *extra):
        with tempfile.TemporaryDirectory() as directory:
            command = [
                sys.executable,
                str(
                    PROJECT_ROOT
                    / "src/iris_cre_33/estimate_parking_pv.py"
                ),
                "--pv-report",
                str(PV_REPORT),
                "--output-dir",
                directory,
                *extra,
            ]

            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=result.stdout + result.stderr,
            )

            reports = list(
                Path(directory).glob("parking_pv_scenarios_*.json")
            )
            self.assertEqual(len(reports), 1)

            return json.loads(
                reports[0].read_text(encoding="utf-8")
            )

    def test_source_derivation_is_recorded(self):
        self.assertEqual(self.report["sqm_per_kwp"], 5.5)

        reference = self.report["reference"]
        self.assertTrue(reference["url"].endswith(
            "Abschlussbericht_Stromstudie_Ba-Wue_V05_080224.pdf"
        ))

        derivation = reference["density_derivation"]
        self.assertEqual(
            derivation["source_module_area_sqm"],
            14392257,
        )
        self.assertEqual(
            derivation["source_capacity_mwp"],
            2617,
        )
        self.assertAlmostEqual(
            derivation["derived_sqm_per_kwp"],
            14392257 / (2617 * 1000),
            places=10,
        )
        self.assertEqual(
            derivation["rounded_reference_sqm_per_kwp"],
            5.5,
        )
        self.assertIs(
            derivation["site_specific_validation"],
            False,
        )

    def test_historical_eligibility_is_preserved(self):
        previous = json.loads(
            historical.REPORT_PATH.read_bytes()
        )

        self.assertEqual(
            self.report["records"],
            previous["records"],
        )
        self.assertEqual(
            self.report["scenario_surface_union_sqm"],
            previous["scenario_surface_union_sqm"],
        )
        self.assertEqual(
            self.report["scenario_polygon_count"],
            previous["scenario_polygon_count"],
        )

        old_scenarios = {
            row["name"]: row
            for row in previous["scenarios"]
        }

        for scenario in self.report["scenarios"]:
            old = old_scenarios[scenario["name"]]

            self.assertEqual(
                scenario["assumed_module_covered_area_sqm"],
                old["assumed_module_covered_area_sqm"],
            )
            self.assertAlmostEqual(
                scenario["conditional_capacity_kwp"],
                old["conditional_capacity_kwp"]
                * previous["sqm_per_kwp"]
                / self.report["sqm_per_kwp"],
                delta=0.02,
            )

    def test_cli_default_reproduces_current_results(self):
        generated = self.run_estimator()

        self.assertEqual(generated["sqm_per_kwp"], 5.5)
        self.assertEqual(
            generated["scenarios"],
            self.report["scenarios"],
        )
        self.assertEqual(
            generated["records"],
            self.report["records"],
        )
        self.assertEqual(
            generated["reference"],
            self.report["reference"],
        )

    def test_cli_override_uses_and_labels_custom_density(self):
        generated = self.run_estimator(
            "--sqm-per-kwp",
            "6.0",
        )

        self.assertEqual(generated["sqm_per_kwp"], 6.0)
        self.assertEqual(
            generated["reference"]["density_basis"],
            "User-supplied scenario density",
        )

        for scenario in generated["scenarios"]:
            expected = (
                self.scenario_union.area
                * scenario["assumed_parking_bay_fraction"]
                * scenario["assumed_canopied_fraction_of_bays"]
                / 6.0
            )

            self.assertAlmostEqual(
                scenario["conditional_capacity_kwp"],
                expected,
                delta=0.02,
            )

        self.assertEqual(
            generated["records"],
            self.report["records"],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()