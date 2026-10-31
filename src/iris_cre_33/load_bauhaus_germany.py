import argparse
import json
import math
import os
from pathlib import Path

from dotenv import load_dotenv
import psycopg
from shapely import from_wkt

from reconciled_inputs import build_records


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)

SOURCE = (
    PROJECT_ROOT
    / "data/processed/bauhaus_buildings_consolidated.json"
)

TABLE = "iris_staging.commercial_poi_staging"

REQUIRED_COLUMNS = {
    "source",
    "osm_type",
    "osm_id",
    "feature_type",
    "name",
    "tags",
    "geom",
    "area_sqm",
    "estimated_pv_kwp",
    "association_status",
}

EXPECTED_ROLE = "iris_loader"
EXPECTED_DATABASE = "iris_local"
AREA_TOLERANCE = 0.02


def validate_feature(feature):
    required = {
        "source",
        "osm_type",
        "osm_id",
        "feature_type",
        "tags",
        "area_sqm",
        "geometry_wkt_25832",
        "association_status",
        "estimated_pv_kwp",
    }

    missing = required - set(feature)
    if missing:
        raise ValueError(
            f"Missing required fields: {sorted(missing)}"
        )

    if feature["source"] != "openstreetmap":
        raise ValueError("Unexpected data source")

    if feature["osm_type"] not in {"way", "relation"}:
        raise ValueError("Expected OSM way or relation")

    if feature["feature_type"] != "building":
        raise ValueError("Expected building feature")

    if feature.get("review_status") == "manual_review":
        raise ValueError(
            "Manual-review building must not be loaded"
        )

    geometry = from_wkt(feature["geometry_wkt_25832"])

    if geometry.is_empty:
        raise ValueError("Empty geometry")

    if not geometry.is_valid:
        raise ValueError("Invalid geometry")

    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Expected polygon geometry")

    actual_area = geometry.area
    if not math.isfinite(actual_area) or actual_area <= 0:
        raise ValueError("Geometry area must be positive and finite")

    recorded_area = float(feature["area_sqm"])
    if not math.isfinite(recorded_area) or recorded_area <= 0:
        raise ValueError("Recorded area must be positive and finite")

    difference = abs(actual_area - recorded_area) / actual_area
    if difference > AREA_TOLERANCE:
        raise ValueError(
            "Area differs from projected geometry "
            f"by {difference:.2%}"
        )

    status = feature["association_status"]
    capacity = feature["estimated_pv_kwp"]

    if status == "accepted_screening_association":
        if (
            capacity is None
            or not math.isfinite(capacity)
            or capacity <= 0
        ):
            raise ValueError(
                "Accepted screening estimate must be positive and finite"
            )
    elif capacity is not None:
        raise ValueError(
            "Only accepted associations may populate the PV column"
        )

    return {
        "source": feature["source"],
        "osm_type": feature["osm_type"],
        "osm_id": int(feature["osm_id"]),
        "feature_type": feature["feature_type"],
        "name": feature["tags"].get("name"),
        "tags": feature["tags"],
        "geometry_wkt_25832": feature["geometry_wkt_25832"],
        "area_sqm": round(actual_area, 2),
        "association_status": status,
        "estimated_pv_kwp": capacity,
    }


def load_and_validate_source(source=SOURCE, pv_report=None):
    source = Path(source)

    if not source.is_file():
        raise FileNotFoundError(
            f"Consolidated dataset not found: {source}"
        )

    if pv_report is None:
        raise ValueError(
            "A reconciled --pv-report is required; "
            "no legacy PV fallback"
        )

    features = build_records(source, Path(pv_report))
    validated = []
    seen_ids = set()

    for feature in features:
        record = validate_feature(feature)
        key = (
            record["source"],
            record["osm_type"],
            record["osm_id"],
            record["feature_type"],
        )

        if key in seen_ids:
            raise ValueError(
                f"Duplicate source feature: {key}"
            )

        seen_ids.add(key)
        validated.append(record)

    print("Input validation: PASSED")
    print("Validated buildings:", len(validated))

    counts = {}
    for record in validated:
        status = record["association_status"]
        counts[status] = counts.get(status, 0) + 1

    print("Association status breakdown:")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    capacities = [
        row["estimated_pv_kwp"]
        for row in validated
        if row["estimated_pv_kwp"] is not None
    ]

    print("Accepted screening PV values:", len(capacities))
    print(
        "NULL building capacity values:",
        len(validated) - len(capacities),
    )
    print(
        "Sum of rounded accepted row estimates (kWp):",
        round(sum(capacities), 2),
    )
    print("This is not a national capacity total.")

    return validated


def check_database(conn):
    role = conn.execute(
        "SELECT current_user"
    ).fetchone()[0]

    database = conn.execute(
        "SELECT current_database()"
    ).fetchone()[0]

    print("Connected role:", role)
    print("Connected database:", database)

    if role != EXPECTED_ROLE:
        raise RuntimeError(
            "Refusing to load using an unexpected account"
        )

    if database != EXPECTED_DATABASE:
        raise RuntimeError(
            f"Unexpected database: {database}"
        )

    columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'iris_staging'
              AND table_name = 'commercial_poi_staging'
            """
        )
    }

    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise RuntimeError(
            "Database schema mismatch. "
            f"Missing columns: {sorted(missing)}"
        )

    print("Database schema validation: PASSED")


def load_features(conn, features):
    inserted = 0
    updated = 0

    for feature in features:
        result = conn.execute(
            """
            INSERT INTO iris_staging.commercial_poi_staging
            (
                source,
                osm_type,
                osm_id,
                feature_type,
                name,
                tags,
                geom,
                area_sqm,
                estimated_pv_kwp,
                association_status
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s::jsonb,
                ST_Multi(
                    ST_GeomFromText(%s, 25832)
                ),
                %s, %s, %s
            )
            ON CONFLICT (
                source,
                osm_type,
                osm_id,
                feature_type
            )
            DO UPDATE SET
                name = EXCLUDED.name,
                tags = EXCLUDED.tags,
                geom = EXCLUDED.geom,
                area_sqm = EXCLUDED.area_sqm,
                estimated_pv_kwp = EXCLUDED.estimated_pv_kwp,
                association_status = EXCLUDED.association_status,
                updated_at = NOW()
            RETURNING (xmax = 0) AS was_inserted
            """,
            (
                feature["source"],
                feature["osm_type"],
                feature["osm_id"],
                feature["feature_type"],
                feature["name"],
                json.dumps(feature["tags"]),
                feature["geometry_wkt_25832"],
                feature["area_sqm"],
                feature["estimated_pv_kwp"],
                feature["association_status"],
            ),
        )

        was_inserted = result.fetchone()[0]
        if was_inserted:
            inserted += 1
        else:
            updated += 1

    return inserted, updated


def main():
    parser = argparse.ArgumentParser(
        description="Load reconciled building candidates into staging."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs without connecting to PostGIS",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=SOURCE,
    )
    parser.add_argument(
        "--pv-report",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    features = load_and_validate_source(
        args.input.resolve(),
        args.pv_report.resolve(),
    )

    if args.dry_run:
        print("\nDRY RUN COMPLETE")
        print("No database connection or writes performed.")
        return

    db_uri = os.environ.get("DB_URI")
    if not db_uri:
        raise RuntimeError("DB_URI is not configured")

    with psycopg.connect(
        db_uri,
        connect_timeout=10,
    ) as conn:
        conn.execute("SET LOCAL statement_timeout = '60s'")
        conn.execute("SET LOCAL lock_timeout = '15s'")
        check_database(conn)

        # One transaction commits all building upserts or none.
        inserted, updated = load_features(conn, features)

    print("\nCONSOLIDATED DATABASE LOAD SUMMARY")
    print("Input building polygons:", len(features))
    print("Inserted:", inserted)
    print("Updated:", updated)
    print("Load completed successfully.")


if __name__ == "__main__":
    main()