import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

from shapely import from_wkt
from shapely.ops import unary_union


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0, str(PROJECT_ROOT / "src/iris_cre_33")
)

from estimate_parking_pv import classify_parking_identity
from load_parking import read_cologne, read_nationwide
from reconciled_inputs import build_records, resolve_reference


REPORT_PATH = PROJECT_ROOT / (
    "data/processed/"
    "parking_pv_scenarios_2938d366aa7d4ef693e39f6df8964d71.json"
)


class TestParkingIdentity(unittest.TestCase):
    def assert_withheld(self, tags):
        review = classify_parking_identity(tags)
        self.assertNotEqual(
            review["status"],
            "explicit_bauhaus_tag_candidate",
        )

    def test_other_retailers_are_withheld(self):
        for operator in (
            "IKEA", "A.T.U", "Media Markt", "Aldi Süd", "Lidl",
        ):
            with self.subTest(operator=operator):
                self.assert_withheld({
                    "operator": operator,
                })

    def test_conflict_overrides_bauhaus_name(self):
        self.assert_withheld({
            "name": "Bauhaus",
            "operator": "IKEA",
        })

    def test_shared_parking_is_withheld(self):
        for name in (
            "IKEA / Bauhaus Moorfleet",
            "Bauhaus + Lidl",
        ):
            with self.subTest(name=name):
                self.assert_withheld({"name": name})

    def test_unknown_operator_is_withheld(self):
        self.assert_withheld({
            "name": "Bauhaus",
            "operator": "Parking company",
        })

    def test_no_identity_is_withheld(self):
        self.assert_withheld({
            "amenity": "parking",
            "parking": "surface",
        })

    def test_naturbauhaus_is_not_the_bauhaus_brand(self):
        self.assert_withheld({
            "name": "Naturbauhaus",
        })

    def test_unreviewed_suffix_is_withheld(self):
        self.assert_withheld({
            "name": "Bauhaus UnknownRetailer",
        })

    def test_conflicting_wikidata_is_withheld(self):
        self.assert_withheld({
            "operator": "Bauhaus",
            "brand:wikidata": "Q999999",
        })

    def test_explicit_bauhaus_tags_can_qualify(self):
        for tags in (
            {"operator": "Bauhaus"},
            {"operator": "Fa. bauhaus"},
            {"brand": "BAUHAUS"},
            {"name": "Bauhaus-Parkplatz"},
        ):
            with self.subTest(tags=tags):
                self.assertEqual(
                    classify_parking_identity(tags)["status"],
                    "explicit_bauhaus_tag_candidate",
                )


class TestParkingPV(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads(
            REPORT_PATH.read_bytes()
        )

        paths = []
        for reference in cls.report["input_files"]:
            path = resolve_reference(
                reference["path"], PROJECT_ROOT
            )
            digest = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()

            if digest != reference["sha256"]:
                raise ValueError(
                    f"Input hash mismatch: {path.name}"
                )

            paths.append(path)

        if len(paths) != 4:
            raise ValueError(
                "Expected four recorded inputs"
            )

        (
            building_path,
            pv_path,
            cologne_path,
            nationwide_path,
        ) = paths

        cologne, _, rejected = read_cologne(
            cologne_path
        )
        if rejected:
            raise ValueError(
                "Rejected parking input geometry"
            )

        nationwide, _ = read_nationwide(
            nationwide_path
        )
        cls.parking = dict(cologne)
        cls.parking.update(nationwide)

        buildings = build_records(
            building_path, pv_path, PROJECT_ROOT
        )
        cls.building_mask = unary_union([
            from_wkt(row["geometry_wkt_25832"])
            for row in buildings
        ])

        cls.stores = {
            row["tags"]["_iris_reconciliation"][
                "official_store_id"
            ]: {
                "name": row["tags"]["_iris_reconciliation"][
                    "official_name"
                ],
                "geometry": from_wkt(
                    row["geometry_wkt_25832"]
                ),
                "status": row["association_status"],
            }
            for row in buildings
            if row["association_status"] in {
                "accepted_screening_association",
                "provisional_association",
            }
        }

        cls.rows = cls.report["records"]

        cls.conflict_mask = unary_union([
            cls.parking[
                (row["osm_type"], row["osm_id"])
            ]["geometry"]
            for row in cls.rows
            if row["identity_review"]["status"] == (
                "conflicting_or_ambiguous_identity_review"
            )
        ])

        cls.scenario_shapes = [
            cls.parking[
                (row["osm_type"], row["osm_id"])
            ]["geometry"]
            .difference(cls.building_mask)
            .difference(cls.conflict_mask)
            for row in cls.rows
            if row["included_in_conditional_scenario"]
        ]
        cls.scenario_union = unary_union(
            cls.scenario_shapes
        )

    def test_inventory_has_no_duplicate_or_missing_ids(self):
        identifiers = [
            (row["osm_type"], row["osm_id"])
            for row in self.rows
        ]
        self.assertEqual(
            len(identifiers), len(set(identifiers))
        )
        self.assertEqual(
            set(identifiers), set(self.parking)
        )
        self.assertEqual(
            self.report["parking_polygon_count"],
            len(identifiers),
        )

    def test_all_candidate_area_is_a_separate_audit_union(self):
        area = unary_union([
            feature["geometry"]
            for feature in self.parking.values()
        ]).area

        self.assertAlmostEqual(
            self.report["parking_area_union_sqm"],
            area,
            delta=0.02,
        )
        self.assertEqual(
            self.report["identity_rule_version"],
            "bauhaus-explicit-tags-v1",
        )

    def test_recorded_tags_and_identity_reviews_match_sources(self):
        for row in self.rows:
            key = (row["osm_type"], row["osm_id"])
            tags = self.parking[key]["tags"]
            geometry = self.parking[key]["geometry"]

            self.assertEqual(row["tags"], tags)

            names = [
                store["name"]
                for store in self.stores.values()
                if geometry.distance(store["geometry"])
                <= self.report["candidate_radius_m"]
            ]

            self.assertEqual(
                row["identity_review"],
                classify_parking_identity(tags, names),
            )

    def test_included_parking_requires_explicit_bauhaus_and_one_store(self):
        included = 0

        for row in self.rows:
            if not row["included_in_conditional_scenario"]:
                continue

            included += 1

            self.assertEqual(
                row["identity_review"]["status"],
                "explicit_bauhaus_tag_candidate",
            )
            self.assertTrue(any(
                re.search(
                    r"\bbauhaus\b",
                    str(row["tags"].get(key, "")),
                    re.IGNORECASE,
                )
                for key in ("operator", "brand", "name")
            ))

            links = row["candidate_store_links"]
            self.assertEqual(len(links), 1)

            link = links[0]
            store_id = link["official_store_id"]
            self.assertIn(store_id, self.stores)

            geometry = self.parking[
                (row["osm_type"], row["osm_id"])
            ]["geometry"]
            store = self.stores[store_id]

            distance = geometry.distance(
                store["geometry"]
            )

            self.assertLessEqual(
                distance,
                self.report["candidate_radius_m"],
            )
            self.assertAlmostEqual(
                link["distance_m"],
                distance,
                delta=0.011,
            )
            self.assertEqual(
                link["building_association_status"],
                store["status"],
            )
            self.assertIs(
                link["parking_association_verified"],
                False,
            )
            self.assertIn(
                row["review_category"],
                {
                    "near_accepted_building_candidate",
                    "near_provisional_building_candidate",
                },
            )

        self.assertGreater(included, 0)

    def test_conflicts_and_missing_identity_never_contribute(self):
        for row in self.rows:
            if row["identity_review"]["status"] != (
                "explicit_bauhaus_tag_candidate"
            ):
                self.assertIs(
                    row["included_in_conditional_scenario"],
                    False,
                )

            for key in ("brand", "operator"):
                value = row["tags"].get(key)

                if value and not re.search(
                    r"\bbauhaus\b",
                    str(value),
                    re.IGNORECASE,
                ):
                    self.assertIs(
                        row["included_in_conditional_scenario"],
                        False,
                    )

    def test_scenario_area_is_deduplicated(self):
        self.assertEqual(
            self.report["scenario_polygon_count"],
            len(self.scenario_shapes),
        )
        self.assertAlmostEqual(
            self.report["scenario_surface_union_sqm"],
            self.scenario_union.area,
            delta=0.02,
        )

        removed = (
            sum(shape.area for shape in self.scenario_shapes)
            - self.scenario_union.area
        )
        self.assertAlmostEqual(
            self.report["scenario_overlap_removed_sqm"],
            max(0.0, removed),
            delta=0.02,
        )

    def test_scenario_excludes_buildings_and_conflicting_parking(self):
        for mask in (
            self.building_mask,
            self.conflict_mask,
        ):
            self.assertLessEqual(
                self.scenario_union.intersection(mask).area,
                0.01,
            )

    def test_scenario_capacities_match_union_area(self):
        capacities = []

        for scenario in self.report["scenarios"]:
            module_area = (
                self.scenario_union.area
                * scenario["assumed_parking_bay_fraction"]
                * scenario["assumed_canopied_fraction_of_bays"]
            )
            capacity = (
                module_area / self.report["sqm_per_kwp"]
            )

            self.assertAlmostEqual(
                scenario["assumed_module_covered_area_sqm"],
                module_area,
                delta=0.02,
            )
            self.assertAlmostEqual(
                scenario["conditional_capacity_kwp"],
                capacity,
                delta=0.02,
            )

            capacities.append(capacity)

        self.assertEqual(
            capacities, sorted(capacities)
        )

    def test_unverified_results_are_not_promoted(self):
        self.assertEqual(
            self.report["verified_parking_ownership_count"],
            0,
        )
        self.assertIsNone(
            self.report["accepted_parking_capacity_kwp"]
        )
        self.assertIsNone(
            self.report["combined_rooftop_and_parking_total"]
        )

        for row in self.rows:
            for key in (
                "ownership_verified",
                "parking_association_verified",
                "installation_feasibility_verified",
            ):
                self.assertIs(row[key], False)


if __name__ == "__main__":
    unittest.main()