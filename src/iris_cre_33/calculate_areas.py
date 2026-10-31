
import argparse
import json
from pathlib import Path

try:
    from .geometry import calculate_footprint
    from .estimates import estimate_pv_kwp
except ImportError:
    from geometry import calculate_footprint
    from estimates import estimate_pv_kwp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    with Path(args.input).open("r", encoding="utf-8") as file:
        data = json.load(file)

    processed = 0
    building_count = 0
    unverified_count = 0
    failed = 0

    for element in data["elements"]:
        osm_id = element.get("id", "UNKNOWN")
        tags = element.get("tags", {})

        try:
            geometry, area_sqm = calculate_footprint(
                element.get("geometry", [])
            )

            processed += 1
            building_tag = tags.get("building")

            if building_tag and building_tag != "no":
                pv = estimate_pv_kwp(area_sqm)
                building_count += 1

                print(
                    f"{osm_id}: "
                    f"footprint_area_sqm={area_sqm:.2f} | "
                    f"estimated_pv_kwp={pv['estimated_pv_kwp']:.2f} | "
                    f"building={building_tag}"
                )

            else:
                unverified_count += 1

                print(
                    f"{osm_id}: "
                    f"area_sqm={area_sqm:.2f} | "
                    "NOT VERIFIED AS BUILDING"
                )

        except (ValueError, TypeError) as exc:
            failed += 1
            print(f"{osm_id}: FAILED — {exc}")

    print("\nSUMMARY")
    print(f"Processed geometries: {processed}")
    print(f"Building-tagged geometries: {building_count}")
    print(f"Unverified building geometries: {unverified_count}")
    print(f"Failed geometries: {failed}")


if __name__ == "__main__":
    main()
