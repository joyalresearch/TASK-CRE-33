
import argparse
import json
from pathlib import Path

try:
    from .database import load_features
    from .geometry import calculate_footprint
    from .estimates import estimate_pv_kwp
except ImportError:
    from database import load_features
    from geometry import calculate_footprint
    from estimates import estimate_pv_kwp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    with Path(args.input).open("r", encoding="utf-8") as file:
        elements = json.load(file)["elements"]

    features = []
    rejected = 0

    for element in elements:
        tags = element.get("tags", {})

        if (
            element.get("type") != "way"
            or not tags.get("building")
            or tags.get("building") == "no"
        ):
            continue

        try:
            geometry, area_sqm = calculate_footprint(
                element.get("geometry", [])
            )

            pv = estimate_pv_kwp(area_sqm)

            features.append({
                "osm_type": "way",
                "osm_id": element["id"],
                "feature_type": "building",
                "name": tags.get("name"),
                "tags": tags,
                "geometry": geometry,
                "area_sqm": area_sqm,
                "estimated_pv_kwp": pv["estimated_pv_kwp"],
                "association_status": "unverified_operator",
            })

        except (ValueError, KeyError, TypeError) as exc:
            rejected += 1
            print(f"REJECTED {element.get('id')}: {exc}")

    counts = load_features(features)

    print("\nDATABASE LOAD SUMMARY")
    print(f"Eligible building features: {len(features)}")
    print(f"Inserted: {counts['inserted']}")
    print(f"Updated: {counts['updated']}")
    print(f"Database failures: {counts['failed']}")
    print(f"Geometry/input rejections: {rejected}")


if __name__ == "__main__":
    main()
