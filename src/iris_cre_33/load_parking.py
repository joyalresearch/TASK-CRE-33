"""Load deduplicated OSM surface parking from Cologne and nationwide sources.

Run from the repository root. Existing --input-only usage remains supported.
The nationwide file is an inventory of proximity candidates, NOT verified ownership.
"""
import argparse
import json
import math
from pathlib import Path

from shapely import wkt

try:
    from .database import load_features
    from .geometry import calculate_footprint
except ImportError:
    from database import load_features
    from geometry import calculate_footprint


def _validated_feature(identity, tags, geometry, area, status):
    if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
        raise ValueError(f"Invalid or empty polygon for {identity}")
    if geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Unexpected geometry type for {identity}: {geometry.geom_type}")
    if not math.isfinite(area) or area <= 0:
        raise ValueError(f"Invalid area for {identity}")
    if abs(geometry.area - area) / geometry.area > 0.02:
        raise ValueError(f"Area discrepancy >2% for {identity}")
    return {
        "osm_type": identity[0],
        "osm_id": identity[1],
        "feature_type": "parking",
        "name": tags.get("name"),
        "tags": tags,
        "geometry": geometry,
        "area_sqm": area,
        "estimated_pv_kwp": None,
        "association_status": status,
    }


def read_cologne(path):
    elements = json.loads(path.read_text(encoding="utf-8"))["elements"]
    result = {}
    deferred = 0
    rejected = []
    for element in elements:
        tags = element.get("tags") or {}
        if tags.get("amenity") != "parking":
            continue
        if element.get("type") == "relation":
            deferred += 1
            continue
        if element.get("type") != "way" or tags.get("parking") != "surface":
            continue
        identity = ("way", int(element["id"]))
        if identity in result:
            raise ValueError(f"Duplicate Cologne parking identity: {identity}")
        try:
            geometry, area = calculate_footprint(element.get("geometry", []))
            result[identity] = _validated_feature(
                identity, tags, geometry, float(area), "unverified_operator"
            )
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append((identity, str(exc)))
    return result, deferred, rejected


def read_nationwide(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("features"), list) or not isinstance(data.get("matches"), list):
        raise ValueError("Nationwide file must contain features and matches lists")
    result = {}
    for feature in data["features"]:
        identity = (feature["osm_type"], int(feature["osm_id"]))
        if identity in result:
            raise ValueError(f"Duplicate nationwide parking identity: {identity}")
        tags = feature["tags"]
        if (feature.get("source") != "openstreetmap"
                or identity[0] != "way"
                or feature.get("feature_type") != "parking"
                or tags.get("amenity") != "parking"
                or tags.get("parking") != "surface"
                or feature.get("association_status") != "unverified_proximity"
                or feature.get("ownership_verified") is not False):
            raise ValueError(f"Invalid provenance/association tags for {identity}")
        geom = wkt.loads(feature["geometry_wkt_25832"])
        result[identity] = _validated_feature(
            identity, tags, geom, float(feature["area_sqm"]), "unverified_proximity"
        )
    seen_matches = set()
    for match in data["matches"]:
        identity = (match["parking_osm_type"], int(match["parking_osm_id"]))
        building = (match["building_osm_type"], int(match["building_osm_id"]))
        pair = (building, identity)
        if pair in seen_matches:
            raise ValueError(f"Duplicate association: {pair}")
        seen_matches.add(pair)
        if identity not in result:
            raise ValueError(f"Association references absent parking: {identity}")
        distance = float(match["distance_m_epsg25832"])
        if not math.isfinite(distance) or distance < 0 or distance > 150.01:
            raise ValueError(f"Invalid proximity distance for {pair}")
        if match.get("association_status") != "unverified_proximity" or match.get("ownership_verified") is not False:
            raise ValueError(f"Invalid association evidence for {pair}")
    return result, len(seen_matches)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Raw Cologne OSM JSON")
    parser.add_argument("--nationwide", type=Path, help="Nationwide candidate features/matches JSON")
    parser.add_argument("--dry-run", action="store_true", help="Validate only; do not connect to database")
    args = parser.parse_args()

    cologne, deferred, rejected = read_cologne(args.input)
    if rejected:
        for identity, reason in rejected[:10]:
            print(f"REJECTED Cologne parking {identity}: {reason}")
        raise ValueError(f"Refusing incomplete load: {len(rejected)} Cologne geometries rejected")

    nationwide, pairs = ({}, 0)
    if args.nationwide:
        nationwide, pairs = read_nationwide(args.nationwide)
    overlap = set(cologne) & set(nationwide)
    # Prefer nationwide geometry and evidence for overlapping IDs.
    combined = dict(cologne)
    combined.update(nationwide)
    features = [combined[key] for key in sorted(combined)]

    print("\nPARKING INPUT RECONCILIATION")
    print(f"Cologne valid surface parking: {len(cologne)}")
    print(f"Nationwide proximity candidates: {len(nationwide)}")
    print(f"Overlapping OSM identities: {len(overlap)}")
    print(f"Combined unique parking polygons: {len(features)}")
    print(f"Candidate building-parking pairs: {pairs}")
    print(f"Cologne deferred relations: {deferred}")
    print("Verified BAUHAUS ownership: 0")

    if args.dry_run:
        print("DRY RUN COMPLETE: no database connection or writes")
        return

    counts = load_features(features)
    print("\nPARKING DATABASE LOAD SUMMARY")
    print(f"Eligible surface parking polygons: {len(features)}")
    print(f"Inserted: {counts['inserted']}")
    print(f"Updated: {counts['updated']}")
    print(f"Database failures: {counts['failed']}")
    if counts['failed']:
        raise RuntimeError("Database reported failed parking records")


if __name__ == "__main__":
    main()
