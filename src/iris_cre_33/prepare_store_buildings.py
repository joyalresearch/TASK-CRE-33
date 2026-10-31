
import json
from collections import Counter
from pathlib import Path

from shapely import from_wkt
from shapely.strtree import STRtree


BASE_FILE = Path("data/processed/bauhaus_germany_validated.json")
MATCH_DIR = Path("data/processed/store_building_matches")

OUTPUT_FILE = Path("data/processed/bauhaus_buildings_consolidated.json")
REPORT_FILE = Path("data/reports/building_consolidation_report.json")

OVERLAP_THRESHOLD = 0.10
AREA_TOLERANCE = 0.02


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_geometry(wkt, recorded_area):
    geometry = from_wkt(wkt)

    if geometry.is_empty:
        raise ValueError("Empty geometry")

    if not geometry.is_valid:
        raise ValueError("Invalid geometry")

    if geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("Not a polygon geometry")

    actual_area = geometry.area

    if actual_area <= 0:
        raise ValueError("Zero-area geometry")

    difference = abs(actual_area - recorded_area) / actual_area

    if difference > AREA_TOLERANCE:
        raise ValueError(
            f"Area mismatch: {recorded_area} vs {actual_area}"
        )

    return geometry


def main():
    base = read_json(BASE_FILE)

    consolidated = list(base["features"])

    existing_ids = {
        (item["osm_type"], int(item["osm_id"]))
        for item in consolidated
        if item["feature_type"] == "building"
    }

    geometries = [
        from_wkt(item["geometry_wkt_25832"])
        for item in consolidated
        if item["feature_type"] == "building"
    ]

    tree = STRtree(geometries) if geometries else None

    accepted = []
    rejected = []
    duplicates = []
    overlaps = []

    match_files = sorted(MATCH_DIR.glob("node_*.json"))

    for path in match_files:
        result = read_json(path)
        store_node_id = result["store_osm_id"]

        for match in result.get("matches", []):
            way_id = int(match["osm_way_id"])
            key = ("way", way_id)

            if match.get("review_status") != "candidate":
                rejected.append({
                    "store_node_id": store_node_id,
                    "building_way_id": way_id,
                    "reason": match.get("review_reason")
                    or "not_approved_for_staging",
                })
                continue

            if key in existing_ids:
                duplicates.append(way_id)
                continue

            try:
                geometry = validate_geometry(
                    match["geometry_wkt_25832"],
                    float(match["area_sqm"]),
                )
            except Exception as exc:
                rejected.append({
                    "store_node_id": store_node_id,
                    "building_way_id": way_id,
                    "reason": str(exc),
                })
                continue

            overlapping_ids = []

            if tree is not None:
                for index in tree.query(geometry):
                    other = geometries[int(index)]

                    intersection_area = geometry.intersection(other).area

                    if intersection_area <= 0:
                        continue

                    ratio = intersection_area / min(
                        geometry.area,
                        other.area,
                    )

                    if ratio > OVERLAP_THRESHOLD:
                        overlapping_ids.append(int(index))

            if overlapping_ids:
                overlaps.append({
                    "store_node_id": store_node_id,
                    "building_way_id": way_id,
                    "overlapping_base_geometry_indices": overlapping_ids,
                })
                continue

            feature = {
                "source": "openstreetmap",
                "osm_type": "way",
                "osm_id": way_id,
                "feature_type": "building",
                "building_type": match["building_type"],
                "area_sqm": round(geometry.area, 2),
                "geometry_wkt_25832": geometry.wkt,
                "tags": match["building_tags"],
                "association_method": "point_in_polygon",
                "association_status": "spatial_candidate",
                "review_status": "candidate",
                "store_node_id": store_node_id,
                "ownership_verified": False,
            }

            accepted.append(feature)
            consolidated.append(feature)
            existing_ids.add(key)

    report = {
        "base_buildings": len(base["features"]),
        "processed_store_files": len(match_files),
        "new_accepted_buildings": len(accepted),
        "manual_review_or_invalid": len(rejected),
        "duplicate_buildings": len(duplicates),
        "spatial_overlap_flags": len(overlaps),
        "total_consolidated_buildings": len(consolidated),
        "new_accepted_area_sqm": round(
            sum(item["area_sqm"] for item in accepted),
            2,
        ),
        "rejected": rejected,
        "duplicates": duplicates,
        "overlaps": overlaps,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT_FILE.write_text(
        json.dumps(
            {"features": consolidated},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    REPORT_FILE.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\nBAUHAUS BUILDING CONSOLIDATION")
    print("--------------------------------")
    print("Base buildings:", report["base_buildings"])
    print("Store match files:", report["processed_store_files"])
    print("New accepted buildings:", report["new_accepted_buildings"])
    print("Manual review / invalid:", report["manual_review_or_invalid"])
    print("Duplicate buildings:", report["duplicate_buildings"])
    print("Spatial overlap flags:", report["spatial_overlap_flags"])
    print("Total buildings:", report["total_consolidated_buildings"])
    print("New accepted area (sqm):", report["new_accepted_area_sqm"])
    print("Output:", OUTPUT_FILE)
    print("Audit report:", REPORT_FILE)


if __name__ == "__main__":
    main()
