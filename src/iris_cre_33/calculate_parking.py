
import argparse
import json
from collections import Counter
from pathlib import Path

try:
    from .geometry import calculate_footprint
except ImportError:
    from geometry import calculate_footprint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    with Path(args.input).open("r", encoding="utf-8") as file:
        data = json.load(file)

    counts = Counter()
    total_surface_area = 0.0

    for element in data["elements"]:
        osm_type = element.get("type")
        osm_id = element.get("id")
        tags = element.get("tags", {})

        if tags.get("amenity") != "parking":
            counts["not_parking"] += 1
            continue

        if osm_type != "way":
            counts["relations_deferred"] += 1
            continue

        parking_type = tags.get("parking", "unspecified")

        try:
            _, area_sqm = calculate_footprint(
                element.get("geometry", [])
            )
            counts["valid_ways"] += 1

            if parking_type == "surface":
                counts["surface_ways"] += 1
                total_surface_area += area_sqm

        except (ValueError, TypeError, OverflowError) as exc:
            counts["failed_ways"] += 1
            print(f"FAILED way/{osm_id}: {exc}")

    print("\nPARKING SUMMARY")
    print(f"Valid parking ways: {counts['valid_ways']}")
    print(f"Surface parking ways: {counts['surface_ways']}")
    print(f"Failed parking ways: {counts['failed_ways']}")
    print(f"Deferred relations: {counts['relations_deferred']}")
    print(f"Total mapped surface parking area: {total_surface_area:.2f} m²")
    print("BAUHAUS-associated parking area: NOT YET DETERMINED")


if __name__ == "__main__":
    main()
