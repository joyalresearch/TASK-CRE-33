"""Evidence-handling tests. No network or database access."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "src/iris_cre_33"
    ),
)

from report_existing_solar import (
    assessment,
    local_file,
    read_checked,
)


class SolarReportChecks(unittest.TestCase):
    def test_no_mapping_does_not_establish_zero_solar(self):
        row = assessment(
            120.0,
            True,
            True,
            [],
            [],
        )

        self.assertEqual(
            row["gross_screening_capacity_kwp"],
            120.0,
        )
        self.assertIsNone(row["existing_solar_coverage_sqm"])
        self.assertIsNone(row["net_additional_capacity_kwp"])
        self.assertEqual(
            row["solar_assessment_status"],
            "no_intersecting_mapping_absence_not_established",
        )

    def test_external_capacity_is_not_deducted(self):
        source = {
            "reported_installed_capacity_kwp": 50.0,
        }

        row = assessment(
            120.0,
            True,
            True,
            [source],
            [],
        )

        self.assertIsNone(row["net_additional_capacity_kwp"])
        self.assertIsNone(row["supported_deduction_sqm"])
        self.assertEqual(
            row["solar_assessment_status"],
            "existing_solar_documented_area_unavailable",
        )

    def test_missing_geometry_preserves_external_evidence(self):
        row = assessment(
            None,
            False,
            False,
            [{"claim": "rooftop PV"}],
            [],
        )

        self.assertEqual(
            row["solar_assessment_status"],
            "geometry_unavailable",
        )
        self.assertEqual(len(row["external_evidence"]), 1)
        self.assertIsNone(row["gross_screening_capacity_kwp"])

    def test_intersection_requires_review(self):
        row = assessment(
            120.0,
            True,
            True,
            [],
            [{"overlap_sqm": 100}],
        )

        self.assertEqual(
            row["solar_assessment_status"],
            "intersecting_solar_candidates_require_review",
        )
        self.assertIsNone(row["net_additional_capacity_kwp"])

    def test_tampered_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            original = b'{"status":"validated"}'
            path.write_bytes(original)

            expected = hashlib.sha256(original).hexdigest()
            read_checked(path, expected)

            path.write_text(
                '{"status":"changed"}',
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                read_checked(path, expected)

    def test_response_path_traversal_rejected(self):
        for name in ("../x", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                local_file(Path("evidence"), name)

    def test_invalid_gross_capacity_rejected(self):
        for value in (
            float("nan"),
            float("inf"),
            -1,
            True,
        ):
            with self.assertRaises(ValueError):
                assessment(
                    value,
                    True,
                    True,
                    [],
                    [],
                )


if __name__ == "__main__":
    unittest.main()