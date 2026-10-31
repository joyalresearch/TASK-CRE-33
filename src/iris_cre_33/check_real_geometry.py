import argparse
import json
from pathlib import Path

from shapely.geometry import Polygon
from shapely.validation import explain_validity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    with Path(args.input).open("r", encoding="utf-8") as file:
        data = json.load(file)

    for element in data["elements"]:
        osm_id = element["id"]
        tags = element.get("tags", {})
        coordinates = element.get("geometry", [])

        if len(coordinates) < 4:
            print(f"{osm_id}: REJECTED — insufficient coordinates")
            continue

        points = [(p["lon"], p["lat"]) for p in coordinates]
        closed = points[0] == points[-1]

        if not closed:
            print(f"{osm_id}: REJECTED — unclosed boundary")
            continue

        polygon = Polygon(points)

        print(
            f"{osm_id}: "
            f"valid={polygon.is_valid}, "
            f"empty={polygon.is_empty}, "
            f"building={tags.get('building', 'NOT TAGGED')}, "
            f"reason={explain_validity(polygon)}"
        )


if __name__ == "__main__":
    main()