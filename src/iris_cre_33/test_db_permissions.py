
import os

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege


def main():
    db_uri = os.environ.get("DB_URI")
    if not db_uri:
        raise RuntimeError("DB_URI is not set.")

    with psycopg.connect(db_uri, connect_timeout=5) as conn:
        role = conn.execute("SELECT current_user").fetchone()[0]
        print("Connected role:", role)

        if role != "iris_loader":
            raise RuntimeError(
                "Permission test must run as iris_loader."
            )

        # Positive test: perform a reversible staging write.
        with conn.transaction():
            conn.execute(
                """
                UPDATE iris_staging.commercial_poi_staging
                SET updated_at = updated_at
                WHERE source = %s
                  AND osm_type = %s
                  AND osm_id = %s
                  AND feature_type = %s
                """,
                ("openstreetmap", "way", 26790316, "building"),
            )
            print("Staging UPDATE: ALLOWED")

        # Negative test: try to create a table outside staging.
        try:
            with conn.transaction():
                conn.execute(
                    sql.SQL(
                        "CREATE TABLE public.iris_permission_test "
                        "(id INTEGER)"
                    )
                )

            raise RuntimeError(
                "SECURITY FAILURE: public CREATE was allowed."
            )

        except InsufficientPrivilege:
            print("Public CREATE: DENIED (expected)")

        print("DATABASE PERMISSION TEST: PASSED")


if __name__ == "__main__":
    main()
