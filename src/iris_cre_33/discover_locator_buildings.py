import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--radius-m", type=int, default=250)
    parser.add_argument("--gap-m", type=float, default=1000)
    args = parser.parse_args()

    if not 0 < args.radius_m <= 1000:
        parser.error("--radius-m must be between 1 and 1000")
    if not math.isfinite(args.gap_m) or args.gap_m <= 0:
        parser.error("--gap-m must be positive and finite")

    source_path = args.capture / "source_response.json"
    report_path = args.capture / "building_reconciliation.csv"
    source_bytes = source_path.read_bytes()
    report_bytes = report_path.read_bytes()

    stores = {
        str(store["store_id"]): store
        for store in json.loads(source_bytes)
    }

    with report_path.open(encoding="utf-8", newline="") as file:
        gaps = [
            row for row in csv.DictReader(file)
            if row["containment_status"] == "no_containment"
            and float(row["nearest_polygon_distance_m"]) > args.gap_m
        ]

    if not gaps:
        raise ValueError("No cases meet the selected gap criteria")

    seeds = []
    statements = []

    for row in gaps:
        store = stores[row["official_store_id"]]
        lat = float(store["location"]["lat"])
        lon = float(store["location"]["lng"])

        if not (
            math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180
        ):
            raise ValueError("Invalid official coordinates")

        seeds.append({
            "official_store_id": store["store_id"],
            "official_name": store["display_name"],
            "lat": lat,
            "lon": lon,
        })

        area = f"(around:{args.radius_m},{lat},{lon})"
        statements.extend([
            f'way{area}["building"]["building"!="no"];',
            f'rel{area}["type"="multipolygon"]'
            '["building"]["building"!="no"];',
        ])

    query = (
        "[out:json][timeout:60];\n(\n"
        + "\n".join(statements)
        + "\n);\nout body geom;"
    )

    folder = args.capture / f"osm_discovery_{uuid.uuid4().hex}"
    folder.mkdir()
    (folder / "query.overpassql").write_bytes(query, encoding="utf-8")

    endpoint = "https://overpass-api.de/api/interpreter"
    manifest = {
        "started_at_utc": utc_now(),
        "endpoint": endpoint,
        "search_radius_m": args.radius_m,
        "gap_selection_threshold_m": args.gap_m,
        "seeds": seeds,
        "source_snapshot_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "reconciliation_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "status": "failed",
        "association_verified": False,
        "limitations": [
            "Local discovery only; not nationwide coverage.",
            "Large enclosing buildings can require a broader spatial query.",
            "Returned relations are not yet assembled into validated polygons.",
            "Nearby buildings are not automatically associated with BAUHAUS.",
        ],
    }

    try:
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode({"data": query}).encode(),
            headers={
                "User-Agent": "IRIS-TASK-CRE-33-research/1.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            response = urllib.request.urlopen(request, timeout=90)
        except urllib.error.HTTPError as error:
            response = error

        with response:
            body = response.read()
            manifest["http_status"] = response.status
            manifest["content_type"] = response.headers.get("Content-Type")

        (folder / "response.bin").write_bytes(body)
        manifest["response_sha256"] = hashlib.sha256(body).hexdigest()

        if manifest["http_status"] != 200:
            raise RuntimeError(
                f"Overpass returned HTTP {manifest['http_status']}"
            )

        result = json.loads(body)
        if result.get("remark"):
            raise RuntimeError(f"Overpass reported: {result['remark']}")

        elements = result.get("elements")
        if not isinstance(elements, list):
            raise ValueError("Missing Overpass elements array")

        counts = Counter(element["type"] for element in elements)
        manifest["element_counts"] = dict(counts)
        manifest["osm_base_timestamp"] = result.get("osm3s", {}).get(
            "timestamp_osm_base"
        )
        manifest["status"] = "captured"

        print("Official gap cases searched:", len(seeds))
        for seed in seeds:
            print(" ", seed["official_store_id"], seed["official_name"])
        print("Returned ways:", counts["way"])
        print("Returned relations:", counts["relation"])

    except Exception as error:
        manifest["error"] = f"{type(error).__name__}: {error}"
        raise

    finally:
        manifest["finished_at_utc"] = utc_now()
        (folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("Evidence saved:", folder.resolve())


if __name__ == "__main__":
    main()