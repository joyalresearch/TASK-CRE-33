
import os

import psycopg
from psycopg.types.json import Jsonb
from shapely.geometry import MultiPolygon


TARGET_TABLE = "iris_staging.commercial_poi_staging"
EXPECTED_DATABASE = "iris_local"
EXPECTED_ROLE = "iris_loader"


def load_features(features):
    """
    Atomically upsert validated OSM features into PostGIS staging.

    If any record fails, the transaction is rolled back and
    an exception is raised.

    Returns inserted, updated, and failed counts.
    """

    db_uri = os.environ.get("DB_URI")

    if not db_uri:
        raise RuntimeError("DB_URI is not configured.")

    counts = {
        "inserted": 0,
        "updated": 0,
        "failed": 0,
    }

    with psycopg.connect(
        db_uri,
        connect_timeout=10,
    ) as conn:

        database_name, role = conn.execute(
            "SELECT current_database(), current_user"
        ).fetchone()

        print("Connected database:", database_name)
        print("Connected role:", role)

        if database_name != EXPECTED_DATABASE:
            raise RuntimeError(
                "Refusing to write to unexpected database: "
                f"{database_name}"
            )

        if role != EXPECTED_ROLE:
            raise RuntimeError(
                "Refusing to write using an unrestricted account"
            )

        if conn.execute(
            "SELECT to_regclass(%s)",
            (TARGET_TABLE,),
        ).fetchone()[0] is None:
            raise RuntimeError(
                "Staging table does not exist."
            )

        statement = """
            INSERT INTO iris_staging.commercial_poi_staging (
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
                'openstreetmap',
                %(osm_type)s,
                %(osm_id)s,
                %(feature_type)s,
                %(name)s,
                %(tags)s,
                ST_Multi(
                    ST_GeomFromText(%(wkt)s, 25832)
                ),
                %(area_sqm)s,
                %(estimated_pv_kwp)s,
                %(association_status)s
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
            RETURNING (xmax = 0) AS inserted
        """

        # psycopg's connection context commits on success
        # and rolls back on any unhandled exception.
        for feature in features:
            try:
                geometry = feature["geometry"]

                if geometry.geom_type == "Polygon":
                    geometry = MultiPolygon([geometry])

                if (
                    geometry.geom_type != "MultiPolygon"
                    or geometry.is_empty
                    or not geometry.is_valid
                ):
                    raise ValueError(
                        "Invalid polygon geometry."
                    )

                if geometry.area <= 0:
                    raise ValueError(
                        "Nonpositive geometry area."
                    )

                parameters = {
                    "osm_type": feature["osm_type"],
                    "osm_id": feature["osm_id"],
                    "feature_type": feature["feature_type"],
                    "name": feature.get("name"),
                    "tags": Jsonb(
                        feature.get("tags", {})
                    ),
                    "wkt": geometry.wkt,
                    "area_sqm": feature["area_sqm"],
                    "estimated_pv_kwp": feature.get(
                        "estimated_pv_kwp"
                    ),
                    "association_status": feature.get(
                        "association_status"
                    ),
                }

                inserted = conn.execute(
                    statement,
                    parameters,
                ).fetchone()[0]

                if inserted:
                    counts["inserted"] += 1
                else:
                    counts["updated"] += 1

            except Exception as exc:
                raise RuntimeError(
                    "Staging load failed for "
                    f"{feature.get('osm_type')}/"
                    f"{feature.get('osm_id')}. "
                    "All writes in this load will be rolled back."
                ) from exc

    return counts
