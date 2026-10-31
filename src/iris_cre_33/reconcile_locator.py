import argparse
import csv
import json
import math
from pathlib import Path

from pyproj import Transformer
from shapely import from_wkt
from shapely.geometry import Point


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    args = parser.parse_args()

    stores = json.loads(
        (args.capture / "source_response.json").read_text(
            encoding="utf-8"
        )
    )
    buildings = json.loads(
        Path(
            "data/processed/bauhaus_buildings_consolidated.json"
        ).read_text(encoding="utf-8")
    )["features"]

    # Official API coordinates are interpreted as longitude/latitude.
    # Project them to the same EPSG:25832 used by our building input.
    transformer = Transformer.from_crs(
        "EPSG:4326", "EPSG:25832", always_xy=True
    )

    polygons = []
    for building in buildings:
        geometry = from_wkt(building["geometry_wkt_25832"])

        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in ("Polygon", "MultiPolygon")
        ):
            raise ValueError(
                f"Unusable building geometry: {building['osm_id']}"
            )

        polygons.append((building, geometry))

    rows = []
    counts = {}
    seen_ids = set()

    for store in stores:
        store_id = store["store_id"]
        if store_id in seen_ids:
            raise ValueError(f"Duplicate official store ID: {store_id}")
        seen_ids.add(store_id)

        lon = float(store["location"]["lng"])
        lat = float(store["location"]["lat"])

        if not (
            math.isfinite(lon)
            and math.isfinite(lat)
            and -180 <= lon <= 180
            and -90 <= lat <= 90
        ):
            raise ValueError(f"Invalid coordinates: {store_id}")

        x, y = transformer.transform(lon, lat, errcheck=True)
        point = Point(x, y)

        containing = [
            building
            for building, geometry in polygons
            if geometry.covers(point)
        ]

        nearest_building, nearest_geometry = min(
            polygons,
            key=lambda pair: pair[1].distance(point),
        )

        if len(containing) == 1:
            status = "single_containment_candidate"
        elif len(containing) > 1:
            status = "multiple_containment_candidates"
        else:
            status = "no_containment"

        counts[status] = counts.get(status, 0) + 1

        rows.append({
            "official_store_id": store_id,
            "official_name": store["display_name"],
            "official_postcode": store["address"]["zipcode"],
            "containment_status": status,
            "containing_osm_ids": ";".join(
                f"{building['osm_type']}/{building['osm_id']}"
                for building in containing
            ),
            "nearest_osm_id": (
                f"{nearest_building['osm_type']}/"
                f"{nearest_building['osm_id']}"
            ),
            "nearest_polygon_distance_m": round(
                nearest_geometry.distance(point), 2
            ),
            "association_verified": False,
        })

    output = args.capture / "building_reconciliation.csv"
    with output.open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print("Official stores examined:", len(rows))
    for status, count in sorted(counts.items()):
        print(f"{status}: {count}")
    print("Saved:", output.resolve())


if __name__ == "__main__":
    main()