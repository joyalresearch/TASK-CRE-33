import copy
import hashlib
import json
from pathlib import Path
import unittest

from shapely.geometry import Polygon

from src.iris_cre_33.assemble_locator_buildings import assemble


FIXTURE = Path(__file__).parent / "fixtures" / "locator_discovery"


class TestLocatorAssembly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (FIXTURE / "response.bin").read_bytes()
        cls.manifest = json.loads(
            (FIXTURE / "manifest.json").read_text(encoding="utf-8")
        )
        cls.elements = {
            (element["type"], element["id"]): element
            for element in json.loads(cls.raw)["elements"]
        }

    def test_fixture_integrity(self):
        self.assertEqual(
            hashlib.sha256(self.raw).hexdigest(),
            self.manifest["response_sha256"],
        )

    def test_split_outer_boundary_is_joined(self):
        relation = self.elements[("relation", 6449013)]

        # This captured relation has two open outer segments.
        outer_members = [
            member for member in relation["members"]
            if member["role"] == "outer"
        ]
        self.assertEqual(len(outer_members), 2)
        for member in outer_members:
            self.assertNotEqual(
                member["geometry"][0],
                member["geometry"][-1],
            )

        geometry, repaired = assemble(relation)
        self.assertTrue(geometry.is_valid)
        self.assertEqual(geometry.geom_type, "Polygon")
        self.assertFalse(repaired)

    def test_schoeneberg_courtyard_remains_a_hole(self):
        relation = self.elements[("relation", 175667)]
        geometry, _ = assemble(relation)

        inner = next(
            member for member in relation["members"]
            if member["role"] == "inner"
        )
        courtyard = Polygon([
            (point["lon"], point["lat"])
            for point in inner["geometry"]
        ])

        self.assertEqual(len(geometry.interiors), 1)
        self.assertFalse(
            geometry.covers(courtyard.representative_point())
        )

    def test_missing_boundary_segment_is_rejected(self):
        relation = copy.deepcopy(
            self.elements[("relation", 6449013)]
        )
        relation["members"].pop()

        with self.assertRaisesRegex(ValueError, "rings"):
            assemble(relation)


if __name__ == "__main__":
    unittest.main()