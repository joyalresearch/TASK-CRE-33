import json
from pathlib import Path

import argparse


def main():
    parser = argparse.ArgumentParser(
        description="Inspect raw OpenStreetMap data"
    )
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    input_path = Path(args.input)

    with input_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    elements = data["elements"]

    print(f"OSM records: {len(elements)}")

    for element in elements:
        geometry = element.get("geometry", [])
        print(
            f"OSM ID: {element['id']} | "
            f"Type: {element['type']} | "
            f"Geometry points: {len(geometry)}"
        )


if __name__ == "__main__":
    main()