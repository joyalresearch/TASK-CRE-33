"""Collect OSM solar evidence around selected roof and parking candidates.

Preview is the default. --live permits bounded, sequential Overpass requests.
Raw evidence only: no geometry association, capacity deduction or database writes.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import certifi
from pyproj import Transformer
from shapely import from_wkt
from shapely.ops import transform

from load_parking import read_cologne, read_nationwide
from reconciled_inputs import build_records

ROOT = Path(__file__).resolve().parents[2]
FILTERS = (
    '["generator:source"~"(^|;)solar(;|$)",i]',
    '["plant:source"~"(^|;)solar(;|$)",i]',
    '["generator:method"="photovoltaic"]',
    '["generator:type"="solar_photovoltaic_panel"]',
    '["roof:material"="solar_panels"]',
)


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    raw = path.read_bytes()
    return json.loads(raw), sha(raw)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def solar_tags(tags):
    source = lambda key: "solar" in {
        v.strip().casefold() for v in str(tags.get(key, "")).split(";")
    }
    return (
        source("generator:source") or source("plant:source")
        or tags.get("generator:method") == "photovoltaic"
        or tags.get("generator:type") == "solar_photovoltaic_panel"
        or tags.get("roof:material") == "solar_panels"
    )


def validate_response(body):
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("remark"):
        raise ValueError("Overpass returned an error/remark, not a complete result")
    if not data.get("osm3s", {}).get("timestamp_osm_base"):
        raise ValueError("OSM snapshot timestamp missing")
    elements = data.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ValueError("Missing elements or terminal count")
    count = elements[-1]
    if count.get("type") != "count":
        raise ValueError("Terminal count missing; partial response possible")
    objects = elements[:-1]
    if int(count["tags"]["total"]) != len(objects):
        raise ValueError("Terminal count differs from returned objects")
    identities = [(e.get("type"), e.get("id")) for e in objects]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate OSM identities in batch")
    if any(kind not in {"node", "way", "relation"} or type(oid) is not int
           for kind, oid in identities):
        raise ValueError("Unexpected OSM object")
    known = set(identities)
    for e in objects:
        if e["type"] == "node":
            lat, lon = e["lat"], e["lon"]
            if not (math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Invalid source coordinates")
        if e["type"] == "way":
            if any(("node", n) not in known for n in e.get("nodes", [])):
                raise ValueError("Incomplete way nodes")
        if e["type"] == "relation":
            if any((m["type"], m["ref"]) not in known
                   for m in e.get("members", [])):
                raise ValueError("Incomplete relation members")
    candidates = [e for e in objects if solar_tags(e.get("tags", {}))]
    return {
        "osm_base_timestamp": data["osm3s"]["timestamp_osm_base"],
        "returned_objects": len(objects),
        "solar_tagged_objects": len(candidates),
        "solar_object_types": dict(Counter(e["type"] for e in candidates)),
        "solar_object_ids": [f"{e['type']}/{e['id']}" for e in candidates],
    }


def plan(args):
    pv, pv_hash = read(args.pv_report)
    parking, parking_hash = read(args.parking_report)
    expected = {"buildings", "pv", "cologne", "nationwide"}
    actual = {}
    for key, path in (("buildings", args.buildings), ("pv", args.pv_report),
                      ("cologne", args.cologne), ("nationwide", args.nationwide)):
        actual[key] = sha(path.read_bytes())
    recorded = {r["sha256"] for r in parking["input_files"]}
    if not all(actual[key] in recorded for key in expected):
        raise ValueError("Parking report does not match current input hashes")
    buildings = build_records(args.buildings, args.pv_report, root=ROOT)
    cologne, _, rejected = read_cologne(args.cologne)
    if rejected:
        raise ValueError("Rejected Cologne geometry; collection refused")
    nationwide, _ = read_nationwide(args.nationwide)
    combined = dict(cologne)
    combined.update(nationwide)
    targets = []
    for row in buildings:
        if row["association_status"] in {
            "accepted_screening_association", "provisional_association"
        }:
            targets.append({
                "key": f"roof/{row['osm_type']}/{row['osm_id']}",
                "geometry": from_wkt(row["geometry_wkt_25832"]),
                "association_status": row["association_status"],
            })
    seen = set()
    for row in parking["records"]:
        if row["included_in_conditional_scenario"]:
            identity = (row["osm_type"], row["osm_id"])
            if identity in seen or identity not in combined:
                raise ValueError("Duplicate/missing selected parking identity")
            seen.add(identity)
            targets.append({
                "key": f"parking/{identity[0]}/{identity[1]}",
                "geometry": combined[identity]["geometry"],
                "association_status": row["review_category"],
            })
    if len(seen) != parking["scenario_polygon_count"]:
        raise ValueError("Parking scenario count mismatch")
    transformer = Transformer.from_crs(25832, 4326, always_xy=True)
    rows = []
    for target in sorted(targets, key=lambda t: t["key"]):
        geom = target.pop("geometry")
        if (geom.is_empty or not geom.is_valid or geom.area <= 0
                or geom.geom_type not in {"Polygon", "MultiPolygon"}):
            raise ValueError("Invalid target geometry")
        # Padding is retrieval context, not an association threshold.
        geo = transform(transformer.transform, geom.buffer(args.padding_m))
        west, south, east, north = geo.bounds
        if not (3 < west < east < 17 and 46 < south < north < 57):
            raise ValueError("Target outside expected Germany coordinate envelope")
        target.update({"geometry_sha256": sha(geom.wkb),
                       "bbox_south_west_north_east": [south, west, north, east]})
        rows.append(target)
    batches = []
    for start in range(0, len(rows), args.batch_size):
        subset = rows[start:start + args.batch_size]
        statements = []
        for row in subset:
            bbox = ",".join(f"{x:.8f}" for x in row["bbox_south_west_north_east"])
            statements.extend(f"nwr{f}({bbox});" for f in FILTERS)
        query = ("[out:json][timeout:90][maxsize:67108864];\n(\n"
                 + "\n".join(statements)
                 + "\n);\n(._; >>;);\nout meta;\nout count;\n")
        batches.append({"index": len(batches) + 1,
                        "targets": [row["key"] for row in subset],
                        "query": query, "query_sha256": sha(query.encode())})
    return {
        "schema_version": "solar-evidence-collection-v1",
        "source": "OpenStreetMap via Overpass", "endpoint": args.endpoint,
        "inputs": {"pv_report_sha256": pv_hash, "parking_report_sha256": parking_hash,
                   **actual},
        "padding_m": args.padding_m, "batch_size": args.batch_size,
        "targets": rows, "batches": batches,
        "limitations": [
            "Search covers selected estimate assets only, not all BAUHAUS facilities.",
            "Bounding-box queries may miss enclosing polygons without a node in the box.",
            "Tag filters and OSM mapping are incomplete; no match does not prove absence.",
            "Solar-tagged plant/roof outlines are not automatically panel coverage.",
            "Solar points indicate presence, not measured area.",
            "Relation membership completeness is not geometry validity or association.",
            "No solar geometry has been accepted for deduction by this collector.",
            "Batches may reflect different OSM snapshot times.",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pv-report", type=Path, required=True)
    p.add_argument("--parking-report", type=Path, required=True)
    p.add_argument("--buildings", type=Path, default=ROOT / "data/processed/bauhaus_buildings_consolidated.json")
    p.add_argument("--cologne", type=Path, default=ROOT / "data/raw/parking_cologne_raw.json")
    p.add_argument("--nationwide", type=Path, default=ROOT / "data/processed/parking_nationwide_candidates.json")
    p.add_argument("--endpoint", default="https://overpass-api.de/api/interpreter")
    p.add_argument("--padding-m", type=float, default=25.0)
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--max-requests", type=int, default=1)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--output-dir", type=Path, default=ROOT / "data/raw/existing_solar")
    args = p.parse_args()
    if (not math.isfinite(args.padding_m) or not 0 <= args.padding_m <= 1000
            or not 1 <= args.batch_size <= 20 or args.max_requests < 0):
        p.error("Invalid padding, batch size or request limit")
    if urllib.parse.urlsplit(args.endpoint).scheme != "https":
        p.error("Endpoint must use HTTPS")
    campaign = plan(args)
    digest = sha(canonical(campaign))
    print("Selected assets:", len(campaign["targets"]))
    print("Search batches:", len(campaign["batches"]))
    print("Plan SHA256:", digest)
    if not args.live:
        print("Preview only: no requests or output writes.")
        print("First batch assets:", campaign["batches"][0]["targets"] if campaign["batches"] else [])
        return
    folder = args.output_dir / digest[:20]
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / "plan.json", campaign)
    context = ssl.create_default_context(cafile=certifi.where())
    sent = 0
    for batch in campaign["batches"]:
        batch_folder = folder / f"batch_{batch['index']:03d}"
        batch_folder.mkdir(exist_ok=True)
        manifest_path = batch_folder / "manifest.json"
        if manifest_path.exists():
            previous, _ = read(manifest_path)
            if previous.get("query_sha256") != batch["query_sha256"]:
                raise ValueError("Cached query mismatch")
            if previous.get("status") == "validated_response":
                raw = (batch_folder / previous["response_file"]).read_bytes()
                if sha(raw) != previous["response_sha256"]:
                    raise ValueError("Cached response hash mismatch")
                validate_response(raw)
                continue
            if not args.retry_failed:
                raise RuntimeError(f"Unresolved batch {batch['index']}; inspect evidence before --retry-failed")
        if sent >= args.max_requests:
            break
        if sent:
            time.sleep(5)
        attempt = uuid.uuid4().hex
        m = {"started_at_utc": now(), "status": "started",
             "query_sha256": batch["query_sha256"], "endpoint": args.endpoint,
             "targets": batch["targets"], "automatic_retries": 0}
        attempt_path = batch_folder / f"attempt_{attempt}.json"
        (batch_folder / "query.overpassql").write_text(batch["query"], encoding="utf-8")
        save(attempt_path, m)
        save(manifest_path, m)
        sent += 1
        print(f"Fetching batch {batch['index']}/{len(campaign['batches'])}", flush=True)
        try:
            req = urllib.request.Request(args.endpoint,
                data=urllib.parse.urlencode({"data": batch["query"]}).encode(),
                headers={"User-Agent": "TASK-CRE-33-solar-evidence/1.0",
                         "Accept": "application/json"}, method="POST")
            try:
                response = urllib.request.urlopen(req, timeout=120, context=context)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read(33554433)
                m["http_status"] = response.status
                m["content_type"] = response.headers.get("Content-Type")
                m["retry_after"] = response.headers.get("Retry-After")
            response_file = f"response_{attempt}.bin"
            (batch_folder / response_file).write_bytes(body)
            m.update(response_file=response_file, response_sha256=sha(body))
            if len(body) > 33554432:
                raise ValueError("Response exceeds 32 MiB; saved prefix is incomplete")
            if m["http_status"] != 200:
                raise RuntimeError(f"Overpass HTTP {m['http_status']}")
            m.update(validate_response(body))
            m["status"] = "validated_response"
            print("Solar-tagged objects:", m["solar_tagged_objects"], m["solar_object_types"], flush=True)
        except BaseException as error:
            m.update(status="unresolved", error_type=type(error).__name__)
            raise
        finally:
            m["finished_at_utc"] = now()
            save(attempt_path, m)
            save(manifest_path, m)
            print("Evidence:", batch_folder.resolve(), flush=True)
    completed = sum(
        1 for b in campaign["batches"]
        if (folder / f"batch_{b['index']:03d}" / "manifest.json").exists()
        and read(folder / f"batch_{b['index']:03d}" / "manifest.json")[0].get("status") == "validated_response"
    )
    save(folder / "summary.json", {
        "updated_at_utc": now(), "plan_sha256": digest,
        "validated_batches": completed, "total_batches": len(campaign["batches"]),
        "requests_this_invocation": sent, "solar_absence_verified": False,
        "net_capacity_calculated": False,
    })
    print("Validated batches:", completed, "/", len(campaign["batches"]))
    print("No estimates or database records changed.")


if __name__ == "__main__":
    main()
