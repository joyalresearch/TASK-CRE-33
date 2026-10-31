import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0, str(PROJECT_ROOT / "src/iris_cre_33")
)

from load_bauhaus_germany import (
    load_and_validate_source,
    load_features,
)
from reconciled_inputs import build_records, META_KEY


BUILDINGS = (
    PROJECT_ROOT
    / "data/processed/bauhaus_buildings_consolidated.json"
)
PV_REPORT = (
    PROJECT_ROOT
    / "data/processed/"
    "reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json"
)


class TestReconciledLoading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads(PV_REPORT.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            cls.records = load_and_validate_source(
                BUILDINGS, PV_REPORT
            )

    def check_modified_report(self, change):
        report = copy.deepcopy(self.report)
        change(report)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pv_report.json"
            path.write_text(
                json.dumps(report), encoding="utf-8"
            )
            return build_records(BUILDINGS, path)

    def test_capacity_values_are_accepted_only(self):
        accepted = [
            row for row in self.records
            if row["association_status"]
            == "accepted_screening_association"
        ]
        expected = [
            row for row in self.report["records"]
            if row["estimate_bucket"] == "accepted"
        ]
        self.assertEqual(len(accepted), len(expected))

        for row in self.records:
            if row["association_status"] == "accepted_screening_association":
                self.assertGreater(row["estimated_pv_kwp"], 0)
            else:
                self.assertIsNone(row["estimated_pv_kwp"])

    def test_provisional_scenarios_stay_separate(self):
        provisional = [
            row for row in self.records
            if row["association_status"]
            == "provisional_association"
        ]
        self.assertTrue(provisional)

        for row in provisional:
            self.assertIsNone(row["estimated_pv_kwp"])
            self.assertGreater(
                row["tags"][META_KEY]["provisional_pv_kwp"],
                0,
            )

    def test_relation_geometry_is_retained(self):
        selected_relations = {
            row["osm_id"]
            for row in self.report["records"]
            if str(row.get("osm_id", "")).startswith("relation/")
        }
        loaded_relations = {
            f"relation/{row['osm_id']}"
            for row in self.records
            if row["osm_type"] == "relation"
        }
        self.assertTrue(selected_relations)
        self.assertTrue(
            selected_relations.issubset(loaded_relations)
        )

    def test_changed_source_hash_rejected(self):
        def change(report):
            report["inputs"]["buildings"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(
            ValueError, "hash mismatch"
        ):
            self.check_modified_report(change)

    def test_dropped_official_record_rejected(self):
        def change(report):
            report["records"].pop()

        with self.assertRaisesRegex(
            ValueError, "dropped or added"
        ):
            self.check_modified_report(change)

    def test_changed_capacity_rejected(self):
        def change(report):
            row = next(
                row for row in report["records"]
                if row["estimate_bucket"] == "accepted"
            )
            row["estimated_pv_kwp"] += 10

        with self.assertRaisesRegex(
            ValueError, "PV estimate differs"
        ):
            self.check_modified_report(change)

    def test_invalid_assumption_rejected(self):
        def change(report):
            report["assumptions"]["usable_fraction"] = 1.5

        with self.assertRaises(ValueError):
            self.check_modified_report(change)

    def test_upsert_preserves_values_and_nulls(self):
        class Cursor:
            def fetchone(self):
                return (False,)

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, values):
                self.calls.append((sql, values))
                return Cursor()

        connection = Connection()
        inserted, updated = load_features(
            connection, self.records
        )

        self.assertEqual(inserted, 0)
        self.assertEqual(updated, len(self.records))

        for record, (sql, values) in zip(
            self.records, connection.calls
        ):
            self.assertIn(
                "iris_staging.commercial_poi_staging", sql
            )
            self.assertEqual(
                values[8], record["estimated_pv_kwp"]
            )

    def test_report_required_for_loading(self):
        with self.assertRaisesRegex(
            ValueError, "reconciled --pv-report"
        ):
            load_and_validate_source(BUILDINGS)


if __name__ == "__main__":
    unittest.main()