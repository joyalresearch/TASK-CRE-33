
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SOURCE = Path("data/raw/bauhaus_germany_pois.json")
OUTPUT_DIR = Path("data/raw/bauhaus_way_batches")
API = "https://api.openstreetmap.org/api/0.6"
BATCH_SIZE = 20

source_data = json.loads(SOURCE.read_text(encoding="utf-8"))
way_ids = sorted({
    int(e["id"])
    for e in source_data["elements"]
    if e["type"] == "way"
})

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def download_batch(ids):
    # /ways?ways=... returns the ways.
    # Node coordinates are fetched through /way/{id}/full
    # when needed, rather than assuming they are included.
    elements = []

    for way_id in ids:
        url = f"{API}/way/{way_id}/full"

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "IRIS-CRE-33/0.1 (OSM research)",
            },
        )

        with urllib.request.urlopen(
            request, timeout=25
        ) as response:
            root = ET.fromstring(response.read())

        nodes = {}
        for node in root.findall("node"):
            nodes[node.attrib["id"]] = {
                "lat": float(node.attrib["lat"]),
                "lon": float(node.attrib["lon"]),
            }

        way = next(
            (
                w for w in root.findall("way")
                if w.attrib["id"] == str(way_id)
            ),
            None,
        )

        if way is None:
            raise ValueError(f"Way {way_id} missing")

        geometry = []
        for nd in way.findall("nd"):
            ref = nd.attrib["ref"]
            if ref not in nodes:
                raise ValueError(
                    f"Missing node {ref} for way {way_id}"
                )
            geometry.append(nodes[ref])

        tags = {
            tag.attrib["k"]: tag.attrib["v"]
            for tag in way.findall("tag")
        }

        elements.append({
            "type": "way",
            "id": way_id,
            "tags": tags,
            "geometry": geometry,
        })

        time.sleep(1)

    return {"elements": elements}


print("Ways to retrieve:", len(way_ids), flush=True)

for start in range(0, len(way_ids), BATCH_SIZE):
    batch = way_ids[start:start + BATCH_SIZE]
    number = start // BATCH_SIZE + 1
    output = OUTPUT_DIR / f"batch_{number:02d}.json"

    if output.exists():
        saved = json.loads(output.read_text(encoding="utf-8"))
        completed = {
            int(e["id"])
            for e in saved.get("elements", [])
            if e.get("type") == "way"
            and e.get("geometry")
        }

        if set(batch).issubset(completed):
            print(
                f"Batch {number}: already complete",
                flush=True,
            )
            continue

    print(
        f"Batch {number}: retrieving {len(batch)} ways",
        flush=True,
    )

    try:
        result = download_batch(batch)

        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(
            f"Batch {number}: saved "
            f"{len(result['elements'])} ways",
            flush=True,
        )

    except Exception as exc:
        print(
            f"Batch {number}: FAILED — {exc}",
            flush=True,
        )
        print(
            "Stopping to avoid unnecessary API requests.",
            flush=True,
        )
        break

print("Download session finished.")
