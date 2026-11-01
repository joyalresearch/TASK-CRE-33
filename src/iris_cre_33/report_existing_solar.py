"""Report gross estimates and unresolved existing-solar deductions.

Reads existing evidence and writes a separate JSON report.
No network requests, database writes or changes to gross estimates.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid


ROOT = Path(__file__).resolve().parents[2]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def read_checked(path, expected=None):
    path = Path(path)
    raw = path.read_bytes()
    actual = digest(raw)

    if expected is not None and actual != expected:
        raise ValueError(f"Evidence hash mismatch: {path.name}")

    return json.loads(raw), actual


def local_file(folder, name):
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or "\\" in name
    ):
        raise ValueError("Invalid evidence filename")

    return folder / name


def assessment(gross, geometry_available, searched, sources, candidates):
    if gross is not None and (
        isinstance(gross, bool)
        or not isinstance(gross, (int, float))
        or not math.isfinite(gross)
        or gross < 0
    ):
        raise ValueError("Invalid gross capacity")

    if not geometry_available:
        status = "geometry_unavailable"
    elif sources:
        status = "existing_solar_documented_area_unavailable"
    elif candidates:
        status = "intersecting_solar_candidates_require_review"
    elif searched:
        status = "no_intersecting_mapping_absence_not_established"
    else:
        status = "outside_selected_solar_search"

    return {
        "gross_screening_capacity_kwp": gross,
        "solar_assessment_status": status,
        "external_evidence": sources,
        "intersecting_solar_candidates": candidates,
        "existing_solar_coverage_sqm": None,
        "supported_deduction_sqm": None,
        "net_additional_capacity_kwp": None,
        "net_status": "unavailable_existing_solar_assessment_incomplete",
    }


def load_collection(folder, pv_hash, parking_hash):
    from collect_existing_solar import validate_response

    plan, plan_hash = read_checked(folder / "plan.json")
    summary, summary_hash = read_checked(folder / "summary.json")

    if summary.get("plan_sha256") != digest(canonical(plan)):
        raise ValueError("Collection plan/summary mismatch")

    if (
        plan["inputs"]["pv_report_sha256"] != pv_hash
        or plan["inputs"]["parking_report_sha256"] != parking_hash
    ):
        raise ValueError(
            "Solar collection used different estimate reports"
        )

    targets = {row["key"]: row for row in plan["targets"]}

    if len(targets) != len(plan["targets"]):
        raise ValueError("Duplicate search targets")

    planned = [
        key
        for batch in plan["batches"]
        for key in batch["targets"]
    ]

    if (
        len(planned) != len(set(planned))
        or set(planned) != set(targets)
    ):
        raise ValueError("Batch target inventory mismatch")

    if (
        summary["validated_batches"] != len(plan["batches"])
        or summary["total_batches"] != len(plan["batches"])
    ):
        raise ValueError("Solar collection is incomplete")

    objects = {}
    selected = set()
    provenance = []
    indices = set()

    for batch in plan["batches"]:
        index = batch["index"]

        if (
            type(index) is not int
            or index < 1
            or index in indices
        ):
            raise ValueError("Invalid batch index")

        indices.add(index)
        base = folder / f"batch_{index:03d}"
        manifest, manifest_hash = read_checked(
            base / "manifest.json"
        )

        saved_query = (
            base / "query.overpassql"
        ).read_text(encoding="utf-8")

        if (
            manifest["status"] != "validated_response"
            or manifest["targets"] != batch["targets"]
            or manifest["query_sha256"] != batch["query_sha256"]
            or digest(batch["query"].encode())
            != batch["query_sha256"]
            or digest(saved_query.encode())
            != batch["query_sha256"]
        ):
            raise ValueError("Invalid batch manifest/query")

        response_path = local_file(
            base,
            manifest["response_file"],
        )

        data, response_hash = read_checked(
            response_path,
            manifest["response_sha256"],
        )

        checked = validate_response(response_path.read_bytes())

        if (
            set(checked["solar_object_ids"])
            != set(manifest["solar_object_ids"])
            or checked["solar_tagged_objects"]
            != manifest["solar_tagged_objects"]
        ):
            raise ValueError("Manifest solar count/IDs mismatch")

        selected.update(checked["solar_object_ids"])

        for obj in data["elements"]:
            if obj["type"] == "count":
                continue

            key = f"{obj['type']}/{obj['id']}"

            if (
                key in objects
                and canonical(objects[key]) != canonical(obj)
            ):
                raise ValueError(
                    f"Conflicting OSM snapshots require review: {key}"
                )

            objects[key] = obj

        provenance.append({
            "manifest_sha256": manifest_hash,
            "response_sha256": response_hash,
            "response_path": str(response_path.resolve()),
            "osm_base_timestamp": checked["osm_base_timestamp"],
        })

    return targets, objects, selected, {
        "plan_sha256": plan_hash,
        "summary_sha256": summary_hash,
        "batches": provenance,
        "search_limitations": plan["limitations"],
    }


def main():
    from pyproj import Transformer
    from shapely import from_wkt
    from shapely.geometry import Point, Polygon
    from shapely.ops import transform, unary_union

    from reconciled_inputs import build_records
    from load_parking import read_cologne, read_nationwide

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--pv-report",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--parking-report",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--collection",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--external-evidence",
        type=Path,
        default=ROOT / (
            "data/reference/existing_solar_evidence.json"
        ),
    )
    parser.add_argument(
        "--buildings",
        type=Path,
        default=ROOT / (
            "data/processed/bauhaus_buildings_consolidated.json"
        ),
    )
    parser.add_argument(
        "--cologne",
        type=Path,
        default=ROOT / "data/raw/parking_cologne_raw.json",
    )
    parser.add_argument(
        "--nationwide",
        type=Path,
        default=ROOT / (
            "data/processed/parking_nationwide_candidates.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/reports/existing_solar",
    )

    args = parser.parse_args()

    pv, pv_hash = read_checked(args.pv_report)
    parking, parking_hash = read_checked(args.parking_report)
    evidence, evidence_hash = read_checked(
        args.external_evidence
    )

    official_ids = {
        row["official_store_id"]
        for row in pv["records"]
    }

    references = {}

    for row in evidence["records"]:
        if (
            row["official_store_id"] not in official_ids
            or row["component"] not in {"roof", "parking"}
            or not row.get("source_url", "").startswith("https://")
            or not row.get("claim")
        ):
            raise ValueError("Invalid external evidence record")

        key = (
            row["official_store_id"],
            row["component"],
        )
        references.setdefault(key, []).append(row)

    targets, objects, selected, collection = load_collection(
        args.collection,
        pv_hash,
        parking_hash,
    )

    buildings = build_records(
        args.buildings,
        args.pv_report,
        root=ROOT,
    )

    shapes = {
        f"{row['osm_type']}/{row['osm_id']}": from_wkt(
            row["geometry_wkt_25832"]
        )
        for row in buildings
    }

    recorded_hashes = {
        item["sha256"]
        for item in parking["input_files"]
    }

    for path in (
        args.buildings,
        args.pv_report,
        args.cologne,
        args.nationwide,
    ):
        if digest(path.read_bytes()) not in recorded_hashes:
            raise ValueError(
                "Parking report/input hashes differ"
            )

    cologne, _, rejected = read_cologne(args.cologne)

    if rejected:
        raise ValueError("Rejected parking geometry")

    nationwide, _ = read_nationwide(args.nationwide)

    combined = dict(cologne)
    combined.update(nationwide)

    building_mask = unary_union(list(shapes.values()))

    conflict_shapes = []

    for row in parking["records"]:
        if row["identity_review"]["status"] == (
            "conflicting_or_ambiguous_identity_review"
        ):
            key = (row["osm_type"], row["osm_id"])
            conflict_shapes.append(combined[key]["geometry"])

    conflict_mask = unary_union(conflict_shapes)
    assets = {}

    for row in pv["records"]:
        if row["estimate_bucket"] in {"accepted", "provisional"}:
            assets[f"roof/{row['osm_id']}"] = shapes[
                row["osm_id"]
            ]

    for row in parking["records"]:
        if row["included_in_conditional_scenario"]:
            key = (row["osm_type"], row["osm_id"])
            geometry = combined[key]["geometry"]
            remaining = geometry.difference(
                building_mask
            ).difference(conflict_mask)

            assets[
                f"parking/{key[0]}/{key[1]}"
            ] = remaining

    if set(assets) != set(targets):
        raise ValueError(
            "Report assets differ from solar search targets"
        )

    for key in assets:
        if key.startswith("roof/"):
            original = shapes[key.removeprefix("roof/")]
        else:
            _, kind, identifier = key.split("/")
            original = combined[
                (kind, int(identifier))
            ]["geometry"]

        if digest(original.wkb) != targets[key]["geometry_sha256"]:
            raise ValueError(
                "Search geometry differs from current geometry"
            )

    parking_union = unary_union([
        geometry
        for key, geometry in assets.items()
        if key.startswith("parking/")
    ])

    if abs(
        parking_union.area
        - parking["scenario_surface_union_sqm"]
    ) > 0.02:
        raise ValueError(
            "Reconstructed eligible parking area differs "
            "from report"
        )

    transformer = Transformer.from_crs(
        4326,
        25832,
        always_xy=True,
    )

    hits = {key: [] for key in assets}
    observations = []

    for key in sorted(selected):
        obj = objects[key]

        record = {
            "osm_id": key,
            "tags": obj.get("tags", {}),
            "intersections": [],
            "accepted_for_deduction": False,
        }

        try:
            if obj["type"] == "node":
                source = Point(obj["lon"], obj["lat"])

            elif obj["type"] == "way":
                refs = obj["nodes"]

                if len(refs) < 4 or refs[0] != refs[-1]:
                    raise ValueError(
                        "Open/incomplete solar outline"
                    )

                source = Polygon([
                    (
                        objects[f"node/{node_id}"]["lon"],
                        objects[f"node/{node_id}"]["lat"],
                    )
                    for node_id in refs
                ])

            else:
                raise ValueError(
                    "Solar relation requires geometry review"
                )

            geometry = transform(
                transformer.transform,
                source,
            )

            if (
                geometry.is_empty
                or not geometry.is_valid
                or not math.isfinite(geometry.area)
            ):
                raise ValueError("Invalid solar geometry")

            record["geometry_wkt_25832"] = geometry.wkt
            record["mapped_outline_area_sqm"] = (
                None
                if obj["type"] == "node"
                else round(geometry.area, 2)
            )

            for asset, shape in assets.items():
                area = geometry.intersection(shape).area

                point_inside = (
                    obj["type"] == "node"
                    and shape.covers(geometry)
                )

                if area > 0 or point_inside:
                    hit = {
                        "solar_osm_id": key,
                        "overlap_sqm": (
                            None
                            if obj["type"] == "node"
                            else round(area, 2)
                        ),
                        "evidence_type": (
                            "point_presence"
                            if obj["type"] == "node"
                            else "polygon_candidate"
                        ),
                        "association_verified": False,
                    }

                    hits[asset].append(hit)
                    record["intersections"].append({
                        "asset_key": asset,
                        **hit,
                    })

            record["review_status"] = (
                "candidate_review_required"
                if record["intersections"]
                else "outside_selected_assets"
            )

        except (ValueError, KeyError) as error:
            record.update({
                "review_status": "geometry_review_required",
                "reason": str(error),
            })

        observations.append(record)

    roof_rows = []

    for row in pv["records"]:
        asset = (
            f"roof/{row['osm_id']}"
            if row.get("osm_id")
            else None
        )

        roof_rows.append({
            "official_store_id": row["official_store_id"],
            "official_name": row["official_name"],
            "osm_id": row.get("osm_id"),
            "association_status": row["association_status"],
            "estimate_bucket": row["estimate_bucket"],
            "footprint_area_sqm": row["footprint_area_sqm"],
            **assessment(
                row["estimated_pv_kwp"],
                row.get("osm_id") in shapes,
                asset in targets,
                references.get(
                    (row["official_store_id"], "roof"),
                    [],
                ),
                hits.get(asset, []),
            ),
        })

    parking_rows = []

    for row in parking["records"]:
        asset = (
            f"parking/{row['osm_type']}/{row['osm_id']}"
        )

        sources = [
            source
            for link in row["candidate_store_links"]
            for source in references.get(
                (link["official_store_id"], "parking"),
                [],
            )
        ]

        parking_rows.append({
            "osm_type": row["osm_type"],
            "osm_id": row["osm_id"],
            "review_category": row["review_category"],
            "included_in_conditional_scenario": (
                row["included_in_conditional_scenario"]
            ),
            "candidate_store_links": row["candidate_store_links"],
            "eligible_surface_area_sqm": (
                round(assets[asset].area, 2)
                if asset in assets
                else None
            ),
            **assessment(
                None,
                True,
                asset in targets,
                sources,
                hits.get(asset, []),
            ),
        })

    scenarios = [
        {
            "scenario": scenario["name"],
            "gross_conditional_capacity_kwp": (
                scenario["conditional_capacity_kwp"]
            ),
            "supported_deduction_sqm": None,
            "net_additional_capacity_kwp": None,
        }
        for scenario in parking["scenarios"]
    ]

    result = {
        "schema_version": "existing-solar-assessment-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "area_crs": "EPSG:25832",
        "inputs": {
            "pv_report": {
                "path": str(args.pv_report.resolve()),
                "sha256": pv_hash,
            },
            "parking_report": {
                "path": str(args.parking_report.resolve()),
                "sha256": parking_hash,
            },
            "external_evidence": {
                "path": str(args.external_evidence.resolve()),
                "sha256": evidence_hash,
            },
            "solar_collection": collection,
        },
        "summary": {
            "roof_records": len(roof_rows),
            "parking_records": len(parking_rows),
            "searched_assets": len(targets),
            "unique_solar_objects": len(selected),
            "intersecting_solar_objects": sum(
                bool(row["intersections"])
                for row in observations
            ),
            "accepted_solar_deduction_objects": 0,
            "roof_status_counts": dict(Counter(
                row["solar_assessment_status"]
                for row in roof_rows
            )),
            "parking_status_counts": dict(Counter(
                row["solar_assessment_status"]
                for row in parking_rows
            )),
        },
        "gross_rooftop_aggregates": pv["aggregates"],
        "parking_scenarios": scenarios,
        "roof_records": roof_rows,
        "parking_records": parking_rows,
        "solar_observations": observations,
        "net_rooftop_total_kwp": None,
        "net_parking_total_kwp": None,
        "limitations": [
            "Evidence/status report; numerical gross-to-net "
            "deductions are not implemented.",
            "No automatic deductions are accepted; net capacity "
            "and deduction area remain null.",
            "Zero intersecting mapping is not evidence of zero "
            "existing solar.",
            "External capacity/module counts do not establish "
            "occupied roof area.",
            "Plant/roof outlines and third-party tags require "
            "review before area deductions.",
            "Gross screening assumptions and original association "
            "decisions are unchanged.",
            "Roof evidence is not transferred to parking; "
            "components are assessed separately.",
            "Parking gross capacity is reported by union-based "
            "scenario, not allocated per polygon.",
            "External URLs are referenced; full source-page "
            "snapshots are not bundled here.",
        ],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)

    output = args.output_dir / (
        f"existing_solar_assessment_{uuid.uuid4().hex}.json"
    )

    with output.open("x", encoding="utf-8") as handle:
        json.dump(
            result,
            handle,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )

    print(json.dumps(
        result["summary"],
        ensure_ascii=False,
        indent=2,
    ))
    print("Numerical net capacity: unavailable")
    print("Gross estimates and database unchanged.")
    print("Saved:", output.resolve())


if __name__ == "__main__":
    main()