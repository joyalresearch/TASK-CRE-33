import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math

from pyproj import Transformer
from shapely import make_valid, transform
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import linemerge, unary_union


def coordinates(items):
    points = [(float(p["lon"]), float(p["lat"])) for p in items]
    if len(points) < 2 or not all(
        math.isfinite(x) and math.isfinite(y)
        and -180 <= x <= 180 and -90 <= y <= 90
        for x, y in points
    ):
        raise ValueError("Invalid longitude/latitude coordinates")
    return points


def assemble_relation(element):
    groups = {"outer": [], "inner": []}

    for member in element.get("members", []):
        role = member.get("role")
        if member.get("type") != "way" or role not in groups:
            raise ValueError("Unsupported relation member or role")

        groups[role].append(
            LineString(coordinates(member["geometry"]))
        )

    rings = {}
    for role, lines in groups.items():
        if not lines:
            rings[role] = []
            continue

        merged = linemerge(MultiLineString(lines))
        parts = (
            list(merged.geoms)
            if merged.geom_type == "MultiLineString"
            else [merged]
        )

        if not all(part.is_ring for part in parts):
            raise ValueError(f"Incomplete or nonsimple {role} rings")

        rings[role] = [Polygon(part.coords) for part in parts]

    if not rings["outer"]:
        raise ValueError("Relation has no outer ring")

    outer = unary_union(rings["outer"])
    inner = unary_union(rings["inner"])

    if not inner.is_empty and not outer.covers(inner):
        raise ValueError("Inner ring extends outside outer geometry")

    return outer.difference(inner)


def assemble(element):
    if element["type"] == "way":
        points = coordinates(element["geometry"])
        if len(points) < 4 or points[0] != points[-1]:
            raise ValueError("Building way is not a closed polygon")
        geometry = Polygon(points)

    elif (
        element["type"] == "relation"
        and element.get("tags", {}).get("type") == "multipolygon"
    ):
        geometry = assemble_relation(element)

    else:
        raise ValueError("Unsupported OSM element")

    repaired = not geometry.is_valid
    if repaired:
        geometry = make_valid(geometry)

    if (
        geometry.is_empty
        or not geometry.is_valid
        or geometry.geom_type not in ("Polygon", "MultiPolygon")
    ):
        raise ValueError("Assembly did not produce valid polygon geometry")

    return geometry, repaired


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads(
        (args.discovery / "manifest.json").read_text(encoding="utf-8")
    )
    raw = (args.discovery / "response.bin").read_bytes()
    response_hash = hashlib.sha256(raw).hexdigest()

    if response_hash != manifest["response_sha256"]:
        raise ValueError("Response hash differs from discovery manifest")

    if manifest.get("status") != "captured":
        raise ValueError("Discovery was not successfully captured")

    elements = json.loads(raw)["elements"]
    transformer = Transformer.from_crs(
        "EPSG:4326", "EPSG:25832", always_xy=True
    )

    features = []
    geometries = []
    rejected = []

    for element in elements:
        identity = f"{element['type']}/{element['id']}"

        try:
            geometry, repaired = assemble(element)
            projected = transform(
                geometry,
                transformer.transform,
                interleaved=False,
            )

            if (
                projected.is_empty
                or not projected.is_valid
                or not math.isfinite(projected.area)
                or projected.area <= 0
            ):
                raise ValueError("Invalid projected geometry or area")

            features.append({
                "osm_type": element["type"],
                "osm_id": element["id"],
                "tags": element.get("tags", {}),
                "geometry_wkt_25832": projected.wkt,
                "area_sqm": round(projected.area, 2),
                "geometry_repaired": repaired,
                "association_verified": False,
            })
            geometries.append(projected)

        except Exception as error:
            rejected.append({
                "osm_identity": identity,
                "reason": f"{type(error).__name__}: {error}",
            })

    matches = []
    for seed in manifest["seeds"]:
        x, y = transformer.transform(
            seed["lon"], seed["lat"], errcheck=True
        )
        point = Point(x, y)

        candidates = [
            f"{feature['osm_type']}/{feature['osm_id']}"
            for feature, geometry in zip(features, geometries)
            if geometry.covers(point)
        ]

        matches.append({
            "official_store_id": seed["official_store_id"],
            "official_name": seed["official_name"],
            "containing_candidates": candidates,
            "association_verified": False,
        })

    output = args.discovery / "assembled_candidates.json"
    with output.open("x", encoding="utf-8") as file:
        json.dump({
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "response_sha256": response_hash,
            "input_crs_assumption": "OSM longitude/latitude, EPSG:4326",
            "output_crs": "EPSG:25832",
            "features": features,
            "rejections": rejected,
            "store_matches": matches,
        }, file, ensure_ascii=False, indent=2)

    counts = Counter(feature["osm_type"] for feature in features)
    print("Response hash: PASSED")
    print("Assembled ways:", counts["way"])
    print("Assembled relations:", counts["relation"])
    print("Rejected elements:", len(rejected))

    for match in matches:
        print(
            match["official_store_id"],
            match["official_name"],
            "->",
            ", ".join(match["containing_candidates"]) or "NONE",
        )

    print("Saved:", output.resolve())


if __name__ == "__main__":
    main()