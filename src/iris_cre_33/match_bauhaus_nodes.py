
import json
from collections import Counter
from pathlib import Path

from pyproj import Transformer
from shapely import wkt
from shapely.geometry import Point
from shapely.strtree import STRtree

NODES_FILE = Path("data/raw/bauhaus_germany_pois.json")
BUILDINGS_FILE = Path(
    "data/processed/bauhaus_germany_validated.json"
)
OUTPUT = Path(
    "data/processed/bauhaus_node_building_matches.json"
)

nodes = [
    element
    for element in json.loads(
        NODES_FILE.read_text(encoding="utf-8")
    )["elements"]
    if element["type"] == "node"
    and element.get("tags", {}).get("brand:wikidata")
    == "Q672043"
]

buildings = json.loads(
    BUILDINGS_FILE.read_text(encoding="utf-8")
)["features"]

geometries = [
    wkt.loads(feature["geometry_wkt_25832"])
    for feature in buildings
]

tree = STRtree(geometries)

transformer = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:25832",
    always_xy=True,
)

results = []

for node in nodes:
    x, y = transformer.transform(
        node["lon"],
        node["lat"],
    )

    point = Point(x, y)

    # Search for mapped BAUHAUS building footprints
    # within 100 metres of each store point.
    candidate_indices = tree.query(
        point.buffer(100)
    )

    candidates = []

    for index in candidate_indices:
        index = int(index)
        polygon = geometries[index]

        distance = round(
            point.distance(polygon),
            2,
        )

        if distance <= 100:
            feature = buildings[index]

            candidates.append({
                "osm_way_id": feature["osm_id"],
                "distance_m": distance,
                "point_inside_footprint": polygon.covers(point),
                "building_area_sqm": feature["area_sqm"],
                "building_name": feature["tags"].get("name"),
                "brand_wikidata": feature["tags"].get(
                    "brand:wikidata"
                ),
            })

    candidates.sort(
        key=lambda item: item["distance_m"]
    )

    if any(
        candidate["point_inside_footprint"]
        for candidate in candidates
    ):
        status = "inside_mapped_building"
    elif candidates:
        status = "near_mapped_building"
    else:
        status = "no_building_within_100m"

    results.append({
        "osm_node_id": node["id"],
        "name": node["tags"].get("name"),
        "city": node["tags"].get("addr:city"),
        "status": status,
        "candidates": candidates,
    })

OUTPUT.parent.mkdir(
    parents=True,
    exist_ok=True,
)

OUTPUT.write_text(
    json.dumps(
        {
            "search_radius_m": 100,
            "results": results,
        },
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

print("BAUHAUS NODE-TO-BUILDING MATCHING")
print("Verified brand-tagged nodes:", len(nodes))

for status, count in Counter(
    result["status"] for result in results
).items():
    print(f"{status}: {count}")

print("\nNodes without nearby mapped buildings:")

for result in results:
    if result["status"] == "no_building_within_100m":
        print(
            result["osm_node_id"],
            result["city"] or "(city missing)",
        )

print("\nSaved:", OUTPUT)
