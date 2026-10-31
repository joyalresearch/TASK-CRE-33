
import json
from collections import Counter
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Polygon
from shapely import transform
from shapely.validation import explain_validity

INPUT_DIR = Path("data/raw/bauhaus_way_batches")
OUTPUT = Path("data/processed/bauhaus_germany_validated.json")

transformer = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:25832",
    always_xy=True,
)

features = []
rejections = []
building_types = Counter()
seen = set()

for path in sorted(INPUT_DIR.glob("batch_*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))

    for element in data["elements"]:
        osm_id = element["id"]

        if osm_id in seen:
            rejections.append({
                "osm_id": osm_id,
                "reason": "Duplicate OSM way",
            })
            continue

        seen.add(osm_id)

        tags = element.get("tags", {})
        building = tags.get("building")

        if not building or building == "no":
            rejections.append({
                "osm_id": osm_id,
                "reason": "No qualifying building tag",
            })
            continue

        geometry = element.get("geometry", [])

        if len(geometry) < 4:
            rejections.append({
                "osm_id": osm_id,
                "reason": "Insufficient coordinates",
            })
            continue

        coordinates = [
            (point["lon"], point["lat"])
            for point in geometry
        ]

        if coordinates[0] != coordinates[-1]:
            rejections.append({
                "osm_id": osm_id,
                "reason": "Open polygon ring",
            })
            continue

        polygon = Polygon(coordinates)

        if polygon.is_empty or not polygon.is_valid:
            rejections.append({
                "osm_id": osm_id,
                "reason": explain_validity(polygon),
            })
            continue

        projected = transform(polygon,
                              transformer.transform,
                              interleaved=False)

        if not projected.is_valid or projected.area <= 0:
            rejections.append({
                "osm_id": osm_id,
                "reason": "Invalid projected polygon",
            })
            continue

        area_sqm = round(projected.area, 2)

        building_types[building] += 1

        features.append({
            "source": "openstreetmap",
            "osm_type": "way",
            "osm_id": osm_id,
            "feature_type": "building",
            "building_type": building,
            "area_sqm": area_sqm,
            "geometry_wkt_25832": projected.wkt,
            "tags": tags,
        })

OUTPUT.parent.mkdir(parents=True, exist_ok=True)

OUTPUT.write_text(
    json.dumps(
        {
            "features": features,
            "rejections": rejections,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

print("NATIONWIDE GEOMETRY VALIDATION")
print("Input ways:", len(seen))
print("Valid building polygons:", len(features))
print("Rejected or excluded:", len(rejections))
print("Total footprint area (sqm):", round(
    sum(f["area_sqm"] for f in features), 2
))

print("\nBuilding types:")
for building_type, count in building_types.most_common():
    print(f"  {building_type}: {count}")

print("\nRejection reasons:")
for reason, count in Counter(
    r["reason"] for r in rejections
).most_common():
    print(f"  {reason}: {count}")

print("\nSaved:", OUTPUT)
