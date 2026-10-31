
import argparse
import csv
import json
from pathlib import Path

try:
    from .geometry import calculate_footprint
except ImportError:
    from geometry import calculate_footprint


def load_elements(path):
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)["elements"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--buildings", required=True)
    parser.add_argument("--parking", required=True)
    parser.add_argument("--max-distance-m", type=float, default=100.0)
    args = parser.parse_args()

    if not 0 <= args.max_distance_m <= 10000:
        parser.error("--max-distance-m must be between 0 and 10000")

    buildings = []

    for element in load_elements(args.buildings):
        tags = element.get("tags", {})

        if not tags.get("building") or tags["building"] == "no":
            continue

        try:
            geometry, _ = calculate_footprint(
                element.get("geometry", [])
            )
            buildings.append((element["id"], geometry))

        except (KeyError, ValueError, TypeError) as exc:
            print(f"SKIPPED building {element.get('id')}: {exc}")

    parking_candidates = []

    for element in load_elements(args.parking):
        if element.get("type") != "way":
            continue

        tags = element.get("tags", {})

        if (
            tags.get("amenity") != "parking"
            or tags.get("parking") != "surface"
        ):
            continue

        try:
            geometry, area = calculate_footprint(
                element.get("geometry", [])
            )
            parking_candidates.append(
                (element["id"], geometry, area)
            )

        except (KeyError, ValueError, TypeError) as exc:
            print(f"SKIPPED parking {element.get('id')}: {exc}")

    match_rows = []

    for building_id, building_geometry in buildings:
        nearby = []

        for parking_id, parking_geometry, parking_area in parking_candidates:
            distance = building_geometry.distance(parking_geometry)

            if distance <= args.max_distance_m:
                nearby.append(
                    (parking_id, distance, parking_area)
                )

                match_rows.append({
                    "building_osm_id": building_id,
                    "parking_osm_id": parking_id,
                    "distance_m": round(distance, 2),
                    "parking_area_sqm": round(parking_area, 2),
                    "association_status": "unverified_proximity",
                })

        nearby.sort(key=lambda item: (item[1], item[0]))

        print(f"\nBuilding OSM ID: {building_id}")
        print(f"Nearby surface parking candidates: {len(nearby)}")

        for parking_id, distance, area in nearby[:10]:
            print(
                f"  parking way/{parking_id} | "
                f"distance={distance:.1f} m | "
                f"area={area:.1f} m² | "
                "association=UNVERIFIED"
            )

    print("\nMATCHING SUMMARY")
    print(f"Buildings examined: {len(buildings)}")
    print(f"Surface parking polygons examined: {len(parking_candidates)}")
    print(f"Candidate building-parking pairs: {len(match_rows)}")
    print("Verified BAUHAUS parking associations: 0")

    # Save machine-readable evidence.
    output_path = Path("data/processed/parking_candidates.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "building_osm_id",
                "parking_osm_id",
                "distance_m",
                "parking_area_sqm",
                "association_status",
            ],
        )

        writer.writeheader()
        writer.writerows(match_rows)

    print(f"Saved candidate matches to: {output_path}")


if __name__ == "__main__":
    main()
