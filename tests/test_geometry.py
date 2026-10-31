
import unittest

from shapely.geometry import Polygon

from src.iris_cre_33.geometry import calculate_footprint


class TestGeometry(unittest.TestCase):

    def test_valid_closed_polygon(self):
        coordinates = [
            {"lon": 7.0, "lat": 51.0},
            {"lon": 7.001, "lat": 51.0},
            {"lon": 7.001, "lat": 51.001},
            {"lon": 7.0, "lat": 51.0},
        ]

        geometry, area = calculate_footprint(coordinates)

        self.assertTrue(geometry.is_valid)
        self.assertEqual(geometry.geom_type, "Polygon")
        self.assertGreater(area, 0)

    def test_open_polygon_rejected(self):
        coordinates = [
            {"lon": 7.0, "lat": 51.0},
            {"lon": 7.001, "lat": 51.0},
            {"lon": 7.001, "lat": 51.001},
            {"lon": 7.0, "lat": 51.001},
        ]

        with self.assertRaisesRegex(ValueError, "not closed"):
            calculate_footprint(coordinates)

    def test_projected_square_area(self):
        from shapely import transform
        from pyproj import Transformer

        # Known 100 m x 100 m square in EPSG:25832.
        square = Polygon([
            (350000, 5650000),
            (350100, 5650000),
            (350100, 5650100),
            (350000, 5650100),
            (350000, 5650000),
        ])

        to_geographic = Transformer.from_crs(
            "EPSG:25832", "EPSG:4326", always_xy=True
        )

        geographic = transform(
            square,
            to_geographic.transform,
            interleaved=False,
        )

        # Convert into the same coordinate format as OSM.
        coordinates = [
            {"lon": x, "lat": y}
            for x, y in geographic.exterior.coords
        ]

        geometry, area = calculate_footprint(coordinates)

        self.assertTrue(geometry.is_valid)
        self.assertAlmostEqual(area, 10000, delta=0.01)

    def test_invalid_coordinates_rejected(self):
        coordinates = [
            {"lon": 7.0, "lat": 51.0},
            {"lon": 999.0, "lat": 51.0},
            {"lon": 7.001, "lat": 51.001},
            {"lon": 7.0, "lat": 51.0},
        ]

        with self.assertRaisesRegex(ValueError, "outside valid"):
            calculate_footprint(coordinates)


if __name__ == "__main__":
    unittest.main()
