
import os
import psycopg


def main():
    db_uri = os.environ.get("DB_URI")

    if not db_uri:
        raise RuntimeError("DB_URI environment variable is not set.")

    with psycopg.connect(db_uri, connect_timeout=5) as conn:
        database = conn.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        staging_table = conn.execute(
            "SELECT to_regclass(%s)",
            ("iris_staging.commercial_poi_staging",),
        ).fetchone()[0]

        postgis_version = conn.execute(
            "SELECT PostGIS_Version()"
        ).fetchone()[0]

        print("Database:", database)
        print("Staging table:", staging_table)
        print("PostGIS:", postgis_version)

        if staging_table is None:
            raise RuntimeError("Required staging table does not exist.")

        print("DATABASE CONNECTION: PASSED")


if __name__ == "__main__":
    main()
