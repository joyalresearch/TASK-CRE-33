import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv
import psycopg

from load_bauhaus_germany import load_and_validate_source


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)

BUILDING_LOADER = (
    PROJECT_ROOT / "src/iris_cre_33/load_bauhaus_germany.py"
)
PARKING_LOADER = (
    PROJECT_ROOT / "src/iris_cre_33/load_parking.py"
)

BUILDING_INPUT = (
    PROJECT_ROOT
    / "data/processed/bauhaus_buildings_consolidated.json"
)
COLOGNE_INPUT = (
    PROJECT_ROOT / "data/raw/parking_cologne_raw.json"
)
NATIONWIDE_INPUT = (
    PROJECT_ROOT
    / "data/processed/parking_nationwide_candidates.json"
)


def read_json(path):
    if not path.is_file():
        raise FileNotFoundError(f"Missing input file: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def expected_buildings(source, pv_report):
    features = load_and_validate_source(source, pv_report)
    expected = {}
    capacities = {}

    for feature in features:
        key = (
            feature["source"],
            feature["osm_type"],
            feature["osm_id"],
            feature["feature_type"],
        )

        if key in expected:
            raise ValueError(f"Duplicate building: {key}")

        expected[key] = feature["association_status"]
        capacities[key] = feature["estimated_pv_kwp"]

    if not expected:
        raise ValueError("Building inventory is empty")

    return expected, capacities


def expected_parking():
    cologne = read_json(COLOGNE_INPUT)["elements"]
    nationwide = read_json(NATIONWIDE_INPUT)["features"]

    expected = {}
    cologne_ids = set()

    for element in cologne:
        tags = element.get("tags", {})

        if (
            element.get("type") != "way"
            or tags.get("amenity") != "parking"
            or tags.get("parking") != "surface"
        ):
            continue

        key = (
            "openstreetmap",
            "way",
            int(element["id"]),
            "parking",
        )

        if key in cologne_ids:
            raise ValueError(
                f"Duplicate Cologne parking: {key}"
            )

        cologne_ids.add(key)
        expected[key] = "unverified_operator"

    nationwide_ids = set()

    for feature in nationwide:
        key = (
            feature["source"],
            feature["osm_type"],
            int(feature["osm_id"]),
            feature["feature_type"],
        )

        if key in nationwide_ids:
            raise ValueError(
                f"Duplicate nationwide parking: {key}"
            )

        nationwide_ids.add(key)

        if (
            key[0] != "openstreetmap"
            or key[1] != "way"
            or key[3] != "parking"
        ):
            raise ValueError(
                f"Unexpected nationwide parking identity: {key}"
            )

        tags = feature.get("tags", {})

        if (
            tags.get("amenity") != "parking"
            or tags.get("parking") != "surface"
        ):
            raise ValueError(
                f"Unexpected nationwide parking tags: {key}"
            )

        if (
            feature.get("association_status")
            != "unverified_proximity"
            or feature.get("ownership_verified") is not False
        ):
            raise ValueError(
                f"Unexpected nationwide parking evidence: {key}"
            )

        # Preserve proximity classification for overlapping inputs.
        expected[key] = "unverified_proximity"

    if not expected or not nationwide_ids:
        raise ValueError(
            "Parking inventories must not be empty"
        )

    overlap_count = len(cologne_ids & nationwide_ids)
    return expected, len(nationwide_ids), overlap_count


def run_script(script, *arguments):
    print(f"\nRunning: {script}", flush=True)

    subprocess.run(
        [sys.executable, str(script), *arguments],
        check=True,
        cwd=PROJECT_ROOT,
    )


def verify_database(buildings, parking, capacities):
    db_uri = os.environ.get("DB_URI")
    if not db_uri:
        raise RuntimeError("DB_URI is not configured")

    expected = {**buildings, **parking}

    if len(expected) != len(buildings) + len(parking):
        raise RuntimeError(
            "Building and parking identities overlap"
        )

    with psycopg.connect(
        db_uri,
        connect_timeout=10,
    ) as conn:
        database, role = conn.execute(
            "SELECT current_database(), current_user"
        ).fetchone()

        expected_database = os.environ.get(
            "EXPECTED_DATABASE", "iris_local"
        )
        expected_role = os.environ.get(
            "EXPECTED_ROLE", "iris_loader"
        )

        if database != expected_database:
            raise RuntimeError(
                f"Unexpected database: {database}"
            )

        if role != expected_role:
            raise RuntimeError(
                f"Unexpected database role: {role}"
            )

        rows = conn.execute(
            """
            SELECT
                source,
                osm_type,
                osm_id,
                feature_type,
                association_status,
                ST_IsValid(geom),
                ST_SRID(geom),
                ST_IsEmpty(geom),
                ST_Area(geom),
                area_sqm,
                estimated_pv_kwp
            FROM iris_staging.commercial_poi_staging
            WHERE source = 'openstreetmap'
              AND feature_type IN ('building', 'parking')
            """
        ).fetchall()

    actual = {}
    invalid = []
    area_mismatches = []
    capacity_mismatches = []

    for (
        source,
        osm_type,
        osm_id,
        feature_type,
        status,
        is_valid,
        srid,
        is_empty,
        geometry_area,
        stored_area,
        stored_capacity,
    ) in rows:
        key = (
            source,
            osm_type,
            int(osm_id),
            feature_type,
        )

        if key in actual:
            raise RuntimeError(
                f"Duplicate database key: {key}"
            )

        actual[key] = status

        # Parking and unaccepted building estimates must remain NULL.
        expected_capacity = capacities.get(key)

        if expected_capacity is None:
            if stored_capacity is not None:
                capacity_mismatches.append(key)
        elif (
            stored_capacity is None
            or not math.isfinite(float(stored_capacity))
            or abs(float(stored_capacity) - expected_capacity) > 0.011
        ):
            capacity_mismatches.append(key)

        if (
            not is_valid
            or srid != 25832
            or is_empty
            or geometry_area is None
            or not math.isfinite(float(geometry_area))
            or geometry_area <= 0
        ):
            invalid.append(key)

        elif (
            stored_area is None
            or not math.isfinite(float(stored_area))
            or abs(geometry_area - stored_area) / geometry_area > 0.02
        ):
            area_mismatches.append(key)

    missing = set(expected) - set(actual)
    unexpected = set(actual) - set(expected)

    status_mismatches = [
        (key, expected[key], actual[key])
        for key in expected.keys() & actual.keys()
        if expected[key] != actual[key]
    ]

    print("\nPOSTGIS VERIFICATION")
    print("Expected buildings:", len(buildings))
    print("Expected parking:", len(parking))
    print("Actual staging records:", len(actual))
    print("Missing feature IDs:", len(missing))
    print("Unexpected feature IDs:", len(unexpected))
    print("Association mismatches:", len(status_mismatches))
    print("Invalid geometries/SRID:", len(invalid))
    print("Area mismatches (>2%):", len(area_mismatches))
    print("PV/NULL mismatches:", len(capacity_mismatches))

    for label, values in (
        ("Missing IDs", missing),
        ("Unexpected IDs", unexpected),
        ("Association mismatches", status_mismatches),
        ("Invalid geometries", invalid),
        ("Area mismatches", area_mismatches),
        ("PV/NULL mismatches", capacity_mismatches),
    ):
        if values:
            print(f"Sample {label}:", sorted(values)[:10])

    if any((
        missing,
        unexpected,
        status_mismatches,
        invalid,
        area_mismatches,
        capacity_mismatches,
    )):
        raise RuntimeError("PostGIS verification FAILED")

    print("Database verification: PASSED")


def main():
    parser = argparse.ArgumentParser(
        description="Validate and load reconciled staging inputs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs without database writes",
    )
    parser.add_argument(
        "--buildings",
        type=Path,
        default=BUILDING_INPUT,
    )
    parser.add_argument(
        "--pv-report",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    print("PROJECT IRIS - TASK-CRE-33")
    print("Reconciled building and parking staging pipeline")

    building_input = args.buildings.resolve()
    pv_report = args.pv_report.resolve()

    building_args = (
        "--input",
        str(building_input),
        "--pv-report",
        str(pv_report),
    )

    buildings, capacities = expected_buildings(
        building_input, pv_report
    )
    parking, nationwide_count, overlap_count = expected_parking()

    print("\nINPUT INVENTORY")
    print("Building features:", len(buildings))
    print("Nationwide parking candidates:", nationwide_count)
    print("Overlapping Cologne/nationwide IDs:", overlap_count)
    print("Combined unique parking polygons:", len(parking))
    print(
        "Expected staging records:",
        len(buildings) + len(parking),
    )

    print("\nBuilding association breakdown:")
    for status, count in sorted(
        Counter(buildings.values()).items()
    ):
        print(f"  {status}: {count}")

    print("\nParking association breakdown:")
    for status, count in sorted(
        Counter(parking.values()).items()
    ):
        print(f"  {status}: {count}")

    run_script(
        BUILDING_LOADER,
        *building_args,
        "--dry-run",
    )

    parking_args = (
        "--input",
        str(COLOGNE_INPUT),
        "--nationwide",
        str(NATIONWIDE_INPUT),
    )

    run_script(
        PARKING_LOADER,
        *parking_args,
        "--dry-run",
    )

    if args.dry_run:
        print(
            "\nPIPELINE DRY RUN COMPLETE: "
            "no database writes performed"
        )
        return

    run_script(BUILDING_LOADER, *building_args)
    run_script(PARKING_LOADER, *parking_args)

    verify_database(buildings, parking, capacities)

    print("\nPIPELINE COMPLETED SUCCESSFULLY")


if __name__ == "__main__":
    main()