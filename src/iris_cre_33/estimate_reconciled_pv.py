import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from shapely import from_wkt
from shapely.ops import unary_union

from estimates import estimate_pv_kwp


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(
        description="Estimate illustrative PV capacity from reconciled footprints."
    )
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--cached-gap-search", type=Path, required=True)
    parser.add_argument(
        "--buildings",
        type=Path,
        default=PROJECT_ROOT / (
            "data/processed/bauhaus_buildings_consolidated.json"
        ),
    )
    parser.add_argument("--usable-fraction", type=float, default=0.60)
    parser.add_argument("--sqm-per-kwp", type=float, default=5.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data/processed",
    )
    args = parser.parse_args()

    # Validate assumptions even if the input contains no accepted records.
    estimate_pv_kwp(
        1.0, args.usable_fraction, args.sqm_per_kwp
    )

    decisions, decision_hash = read_json(args.decisions)
    buildings, building_hash = read_json(args.buildings)
    gaps, gap_hash = read_json(args.cached_gap_search)

    if decisions.get("cached_gap_search_sha256") != gap_hash:
        raise ValueError("Cached geometry input differs from decision provenance")

    geometries = {}

    def add_geometry(identifier, wkt):
        geometry = from_wkt(wkt)
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            or geometry.area <= 0
        ):
            raise ValueError(f"Unusable geometry: {identifier}")

        if identifier in geometries:
            if not geometries[identifier].equals(geometry):
                raise ValueError(
                    f"Conflicting versions require review: {identifier}"
                )
        else:
            geometries[identifier] = geometry

    for feature in buildings["features"]:
        add_geometry(
            f"{feature['osm_type']}/{feature['osm_id']}",
            feature["geometry_wkt_25832"],
        )

    for candidate in gaps["candidates"]:
        add_geometry(
            candidate["osm_id"],
            candidate["geometry_wkt_25832"],
        )

    rows = []
    seen_stores = set()
    used_polygons = {}
    bucket_geometries = {"accepted": [], "provisional": []}
    bucket_kwp = {"accepted": [], "provisional": []}

    for decision in decisions["decisions"]:
        store_id = decision["official_store_id"]
        if store_id in seen_stores:
            raise ValueError(f"Duplicate official store ID: {store_id}")
        seen_stores.add(store_id)

        status = decision["status"]
        identifier = decision.get("osm_id")
        row = {
            "official_store_id": store_id,
            "official_name": decision["official_name"],
            "association_status": status,
            "osm_id": identifier,
            "estimate_bucket": "unavailable",
            "footprint_area_sqm": None,
            "assumed_usable_area_sqm": None,
            "estimated_pv_kwp": None,
            "association_reasons": decision.get("reasons", []),
            "association_limitations": decision.get("limitations", []),
        }

        if status == "accepted_screening_association":
            if decision.get("association_accepted_for_screening") is not True:
                raise ValueError("Inconsistent accepted-association flag")
            bucket = "accepted"
        elif status == "provisional_association":
            bucket = "provisional"
        else:
            # Parking structures and unresolved cases retain null estimates.
            rows.append(row)
            continue

        if identifier not in geometries:
            raise ValueError(f"Selected geometry missing: {identifier}")
        if identifier in used_polygons:
            raise ValueError(
                f"Polygon selected more than once: {identifier}; "
                "allocation must be resolved before estimation"
            )
        used_polygons[identifier] = store_id

        geometry = geometries[identifier]
        calculated = estimate_pv_kwp(
            geometry.area,
            args.usable_fraction,
            args.sqm_per_kwp,
        )

        row.update({
            "estimate_bucket": bucket,
            "footprint_area_sqm": round(geometry.area, 2),
            "assumed_usable_area_sqm": round(
                calculated["usable_roof_area_sqm"], 2
            ),
            "estimated_pv_kwp": round(
                calculated["estimated_pv_kwp"], 2
            ),
        })
        bucket_geometries[bucket].append(geometry)
        bucket_kwp[bucket].append(calculated["estimated_pv_kwp"])
        rows.append(row)

    aggregates = {}
    for bucket in ("accepted", "provisional"):
        shapes = bucket_geometries[bucket]
        summed_area = sum(shape.area for shape in shapes)
        union_area = unary_union(shapes).area if shapes else 0.0
        overlap = max(0.0, summed_area - union_area)
        tolerance = max(0.01, summed_area * 1e-9)
        safe_total = overlap <= tolerance

        aggregates[bucket] = {
            "store_count": len(shapes),
            "overlapping_footprint_area_sqm": round(overlap, 4),
            "aggregate_withheld_due_to_overlap": not safe_total,
            "illustrative_capacity_kwp": (
                round(sum(bucket_kwp[bucket]), 2) if safe_total else None
            ),
        }

    accepted_union = unary_union(bucket_geometries["accepted"])
    provisional_union = unary_union(bucket_geometries["provisional"])
    cross_overlap = accepted_union.intersection(provisional_union).area

    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "decisions": {
                "path": str(args.decisions.resolve()),
                "sha256": decision_hash,
            },
            "buildings": {
                "path": str(args.buildings.resolve()),
                "sha256": building_hash,
            },
            "cached_gap_search": {
                "path": str(args.cached_gap_search.resolve()),
                "sha256": gap_hash,
            },
        },
        "assumptions": {
            "usable_fraction": args.usable_fraction,
            "sqm_per_kwp": args.sqm_per_kwp,
            "formula": "footprint_area_sqm * usable_fraction / sqm_per_kwp",
            "area_crs": "EPSG:25832",
        },
        "aggregates": aggregates,
        "cross_bucket_overlap_sqm": round(cross_overlap, 4),
        "combined_capacity_total": None,
        "records": rows,
        "limitations": [
            "Footprint-based illustrative capacity, not an engineering assessment.",
            "Accepted refers to screening association, not verified solar capacity.",
            "Provisional estimates assume the candidate footprint is applicable.",
            "Residential or mixed-use roof allocation may be unresolved.",
            "No roof-access, structural, shading or existing-PV assessment.",
            "Unknown capacity is null, not zero.",
            "Accepted and provisional scenarios are not combined.",
            "No national capacity-total or electricity-generation claim.",
        ],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"reconciled_pv_{uuid.uuid4().hex}.json"
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    print("Official stores retained:", len(rows))
    print("Estimate buckets:", dict(Counter(
        row["estimate_bucket"] for row in rows
    )))
    for bucket, summary in aggregates.items():
        print(bucket, ":", json.dumps(summary))
    print("Cross-bucket overlap sqm:", round(cross_overlap, 4))
    print("Combined/national capacity total: not reported")
    print("Saved:", output.resolve())


if __name__ == "__main__":
    main()