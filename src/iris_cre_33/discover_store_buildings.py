
import argparse
import json
import math
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import Transformer
from shapely import transform
from shapely.geometry import Point, Polygon


RAW_DIR = Path("data/raw/osm_store_areas")
OUTPUT_DIR = Path("data/processed/store_building_matches")
SOURCE_FILE = Path("data/raw/bauhaus_germany_pois.json")

API = "https://api.openstreetmap.org/api/0.6/map"
USER_AGENT = "IRIS-CRE-33/0.1 (OSM research)"

BAUHAUS_WIKIDATA_ID = "Q672043"

MANUAL_REVIEW_BUILDING_TYPES = {
    "residential",
    "apartments",
    "house",
    "office",
    "detached",
    "semidetached_house",
}

MIN_CANDIDATE_AREA_SQM = 2000

transformer = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:25832",
    always_xy=True,
)


def fetch_osm_area(node_id, lon, lat):
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    cache = RAW_DIR / f"node_{node_id}.osm"

    if cache.exists():
        print("Using cached OSM response:", cache)
        content = cache.read_bytes()
        ET.fromstring(content)
        return content

    lat_delta = 0.0018
    lon_delta = lat_delta / max(
        0.2,
        abs(math.cos(math.radians(lat))),
    )

    bbox = (
        f"{lon - lon_delta:.7f},"
        f"{lat - lat_delta:.7f},"
        f"{lon + lon_delta:.7f},"
        f"{lat + lat_delta:.7f}"
    )

    url = f"{API}?bbox={bbox}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )

    print("Downloading OSM area for node:", node_id)

    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:
        content = response.read()

    # Save only successfully parsed XML.
    ET.fromstring(content)
    cache.write_bytes(content)

    return content


def evaluate_building(building_type, area_sqm):
    building_type = building_type.lower()

    if building_type in MANUAL_REVIEW_BUILDING_TYPES:
        return (
            "manual_review",
            "non_retail_building_type",
        )

    if area_sqm < MIN_CANDIDATE_AREA_SQM:
        return (
            "manual_review",
            "small_building_footprint",
        )

    return (
        "candidate",
        None,
    )


def discover(node):
    node_id = node["id"]
    lon = node["lon"]
    lat = node["lat"]

    xml_bytes = fetch_osm_area(
        node_id,
        lon,
        lat,
    )

    root = ET.fromstring(xml_bytes)

    nodes = {
        item.attrib["id"]: (
            float(item.attrib["lon"]),
            float(item.attrib["lat"]),
        )
        for item in root.findall("node")
    }

    store_point = Point(lon, lat)
    matches = []

    for way in root.findall("way"):
        tags = {
            tag.attrib["k"]: tag.attrib["v"]
            for tag in way.findall("tag")
        }

        building_type = tags.get("building")

        if not building_type or building_type.lower() == "no":
            continue

        refs = [
            nd.attrib["ref"]
            for nd in way.findall("nd")
        ]

        if not all(ref in nodes for ref in refs):
            continue

        coordinates = [
            nodes[ref]
            for ref in refs
        ]

        if len(coordinates) < 4:
            continue

        if coordinates[0] != coordinates[-1]:
            continue

        polygon = Polygon(coordinates)

        if polygon.is_empty or not polygon.is_valid:
            continue

        if not polygon.covers(store_point):
            continue

        projected = transform(
            polygon,
            transformer.transform,
            interleaved=False,
        )

        area_sqm = round(projected.area, 2)

        review_status, review_reason = evaluate_building(
            building_type,
            area_sqm,
        )

        matches.append({
            "osm_way_id": int(way.attrib["id"]),
            "building_type": building_type,
            "building_name": tags.get("name"),
            "building_tags": tags,
            "association_method": "point_in_polygon",
            "association_status": "spatial_candidate",
            "ownership_verified": False,
            "area_sqm": area_sqm,
            "geometry_wkt_25832": projected.wkt,
            "review_status": review_status,
            "review_reason": review_reason,
        })

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = OUTPUT_DIR / f"node_{node_id}.json"

    result = {
        "store_osm_type": "node",
        "store_osm_id": node_id,
        "store_name": node.get("tags", {}).get("name"),
        "store_brand_wikidata": node.get(
            "tags", {}
        ).get("brand:wikidata"),
        "source": "OpenStreetMap",
        "matches": matches,
    }

    output.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("Store node:", node_id)
    print("Containing buildings:", len(matches))

    for match in matches:
        print(
            "Building way:",
            match["osm_way_id"],
            "| Area:",
            match["area_sqm"],
            "sqm",
            "| Review:",
            match["review_status"],
        )

        if match["review_reason"]:
            print(
                "Review reason:",
                match["review_reason"],
            )

    print("Saved:", output)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--node-id",
        type=int,
        required=True,
    )

    args = parser.parse_args()

    source = json.loads(
        SOURCE_FILE.read_text(
            encoding="utf-8",
        )
    )

    node = next(
        (
            item
            for item in source["elements"]
            if item["type"] == "node"
            and item["id"] == args.node_id
        ),
        None,
    )

    if node is None:
        raise ValueError(
            f"OSM node {args.node_id} not found"
        )

    brand_id = node.get(
        "tags", {}
    ).get("brand:wikidata")

    if brand_id != BAUHAUS_WIKIDATA_ID:
        raise ValueError(
            "Node does not have the expected BAUHAUS brand ID"
        )

    discover(node)


if __name__ == "__main__":
    main()
